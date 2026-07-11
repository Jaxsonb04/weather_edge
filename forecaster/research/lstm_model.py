"""Train LSTM forecasters for the two temperature targets."""

import pandas as pd
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import mean_absolute_error, mean_squared_error
import json
from pathlib import Path

try:
    from .forecast_validation import chronological_unit_split_masks
except ImportError:  # Direct invocation: python research/lstm_model.py
    from forecast_validation import chronological_unit_split_masks

FEATURES_PATH = "weather_features.csv"
MODELS_DIR    = Path("models")
PLOTS_DIR     = Path("plots")
MODELS_DIR.mkdir(exist_ok=True)
PLOTS_DIR.mkdir(exist_ok=True)

SEQ_LEN       = 48    # hours of history per sequence
BATCH_SIZE    = 512
HIDDEN_SIZE   = 48
NUM_LAYERS    = 1
DROPOUT       = 0.1
LEARNING_RATE = 2e-3
MAX_EPOCHS    = 15
PATIENCE      = 3

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
torch.manual_seed(42)
np.random.seed(42)

# Raw weather signals plus calendar encodings. The LSTM learns lag structure
# from the sequence instead of receiving hand-built lag columns.
LSTM_FEATURES = [
    "temp_f", "humidity", "pressure_hpa",
    "wind_speed_mph", "dew_point_f",
    "wind_dir_sin", "wind_dir_cos",
    "hour_sin", "hour_cos",
    "doy_sin", "doy_cos",
    "next_doy_sin", "next_doy_cos",
    # Inland upstream (Concord) lead signal -- validated to cut hot-day MAE
    # ~2.3F with no all-days regression. Dense (100% coverage after the hourly
    # join), so no NaN windows are dropped. Requires features built via
    # features.load_data_with_inland (the weather_features.csv default).
    "inland_temp", "inland_temp_lag_24h", "inland_high_so_far_today",
    "inland_temp_max_24h", "sfo_minus_inland_temp", "sfo_minus_inland_temp_lag_24h",
]


class SequenceDataset(Dataset):
    """Yield windows ending at the forecast timestamp."""
    def __init__(self, features, targets, seq_len):
        self.features = features
        self.targets  = targets
        self.seq_len  = seq_len

        T = len(targets)
        feat_has_nan = np.isnan(features).any(axis=1)
        target_ok    = ~np.isnan(targets)

        cum = np.concatenate([[0], np.cumsum(feat_has_nan.astype(np.int32))])
        t_idx = np.arange(seq_len - 1, T)
        nans_in_window = cum[t_idx + 1] - cum[t_idx + 1 - seq_len]
        valid_mask = (nans_in_window == 0) & target_ok[t_idx]
        self.valid_indices = t_idx[valid_mask].astype(np.int64)

        print(f"    dataset: {len(self.valid_indices):,} valid sequences "
              f"({len(self.valid_indices)/T:.1%} of total)", flush=True)

    def __len__(self):
        return len(self.valid_indices)

    def __getitem__(self, idx):
        t = self.valid_indices[idx]
        X = self.features[t - self.seq_len + 1:t + 1]
        y = self.targets[t]
        return torch.FloatTensor(X), torch.FloatTensor([y])


class LSTMForecaster(nn.Module):
    """Small sequence model with a dense regression head."""
    def __init__(self, input_size, hidden_size, num_layers, dropout):
        super().__init__()
        self.lstm = nn.LSTM(
            input_size=input_size,
            hidden_size=hidden_size,
            num_layers=num_layers,
            dropout=dropout if num_layers > 1 else 0.0,
            batch_first=True,
        )
        self.head = nn.Sequential(
            nn.Linear(hidden_size, hidden_size // 2),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(hidden_size // 2, 1),
        )

    def forward(self, x):
        out, (h_n, c_n) = self.lstm(x)
        last = out[:, -1, :]   # final timestep: (batch, hidden)
        return self.head(last).squeeze(-1)


def prepare_data(df, target, feature_cols, ratios=(0.7, 0.15, 0.15)):
    """Chronological forecast-unit split matching XGBoost; scaler fit on train only."""
    df = df.dropna(subset=[target]).copy()
    masks = chronological_unit_split_masks(df.index, target, ratios)

    X = df[feature_cols].values.astype(np.float32)
    y = df[target].values.astype(np.float32)
    train_mask = masks["train"].to_numpy()

    scaler = StandardScaler()
    X[train_mask] = scaler.fit_transform(X[train_mask])
    X[~train_mask] = scaler.transform(X[~train_mask])

    splits = {
        "train": (
            X[masks["train"].to_numpy()],
            y[masks["train"].to_numpy()],
            df.index[masks["train"].to_numpy()],
        ),
        "val": (
            X[masks["val"].to_numpy()],
            y[masks["val"].to_numpy()],
            df.index[masks["val"].to_numpy()],
        ),
        "test": (
            X[masks["test"].to_numpy()],
            y[masks["test"].to_numpy()],
            df.index[masks["test"].to_numpy()],
        ),
    }
    for name, (Xp, yp, idx) in splits.items():
        print(f"  {name:5s}  {len(Xp):>6,} rows   "
              f"{idx.min().date()} to {idx.max().date()}")
    return splits, scaler


def train_lstm(model, train_loader, val_loader, max_epochs, patience):
    """Train with early stopping on validation MAE."""
    optimizer = torch.optim.Adam(model.parameters(), lr=LEARNING_RATE)
    loss_fn   = nn.L1Loss()

    best_val_mae, best_state, epochs_without_improvement = float("inf"), None, 0

    for epoch in range(max_epochs):
        model.train()
        train_loss_total = 0.0
        for X_batch, y_batch in train_loader:
            X_batch, y_batch = X_batch.to(DEVICE), y_batch.squeeze(-1).to(DEVICE)
            optimizer.zero_grad()
            preds = model(X_batch)
            loss  = loss_fn(preds, y_batch)
            loss.backward()
            optimizer.step()
            train_loss_total += loss.item() * X_batch.size(0)
        train_mae = train_loss_total / len(train_loader.dataset)

        model.eval()
        val_loss_total = 0.0
        with torch.no_grad():
            for X_batch, y_batch in val_loader:
                X_batch, y_batch = X_batch.to(DEVICE), y_batch.squeeze(-1).to(DEVICE)
                val_loss_total += loss_fn(model(X_batch), y_batch).item() * X_batch.size(0)
        val_mae = val_loss_total / len(val_loader.dataset)

        flag = ""
        if val_mae < best_val_mae:
            best_val_mae = val_mae
            best_state   = {k: v.clone() for k, v in model.state_dict().items()}
            epochs_without_improvement = 0
            flag = "  <- new best"
        else:
            epochs_without_improvement += 1

        print(f"  epoch {epoch+1:>2}  train mae {train_mae:5.2f}  val mae {val_mae:5.2f}{flag}")

        if epochs_without_improvement >= patience:
            print(f"  early stopping ({patience} epochs without improvement)")
            break

    model.load_state_dict(best_state)
    return model, best_val_mae


def predict(model, loader):
    """run inference on a dataloader, return concatenated predictions."""
    model.eval()
    all_preds = []
    with torch.no_grad():
        for X_batch, _ in loader:
            X_batch = X_batch.to(DEVICE)
            all_preds.append(model(X_batch).cpu().numpy())
    return np.concatenate(all_preds)


def run_lstm_pipeline(target):
    print(f"\nlstm - target: {target}")
    print(f"device: {DEVICE}")

    df = pd.read_csv(FEATURES_PATH, index_col=0, parse_dates=True)
    print(f"  {len(df):,} rows  using {len(LSTM_FEATURES)} raw features")

    splits, scaler = prepare_data(df, target, LSTM_FEATURES)

    print(f"\nbuilding sequence datasets (seq_len={SEQ_LEN}h)...")
    print("  train:"); train_ds = SequenceDataset(*splits["train"][:2], SEQ_LEN)
    print("  val:  "); val_ds   = SequenceDataset(*splits["val"][:2],   SEQ_LEN)
    print("  test: "); test_ds  = SequenceDataset(*splits["test"][:2],  SEQ_LEN)

    train_loader = DataLoader(train_ds, batch_size=BATCH_SIZE, shuffle=True,  num_workers=0)
    val_loader   = DataLoader(val_ds,   batch_size=BATCH_SIZE, shuffle=False, num_workers=0)
    test_loader  = DataLoader(test_ds,  batch_size=BATCH_SIZE, shuffle=False, num_workers=0)

    print(f"\ntraining lstm (hidden={HIDDEN_SIZE}, layers={NUM_LAYERS}, dropout={DROPOUT})...")
    model = LSTMForecaster(
        input_size=len(LSTM_FEATURES),
        hidden_size=HIDDEN_SIZE,
        num_layers=NUM_LAYERS,
        dropout=DROPOUT,
    ).to(DEVICE)

    n_params = sum(p.numel() for p in model.parameters())
    print(f"  parameters: {n_params:,}")

    model, best_val_mae = train_lstm(model, train_loader, val_loader, MAX_EPOCHS, PATIENCE)

    val_preds   = predict(model, val_loader)
    val_targets = splits["val"][1][val_ds.valid_indices]
    val_index   = splits["val"][2][val_ds.valid_indices]
    val_mae     = mean_absolute_error(val_targets, val_preds)
    print(f"\n  val   mae {val_mae:.2f}F")

    test_preds   = predict(model, test_loader)
    test_targets = splits["test"][1][test_ds.valid_indices]
    test_index   = splits["test"][2][test_ds.valid_indices]
    test_mae     = mean_absolute_error(test_targets, test_preds)
    test_rmse    = mean_squared_error(test_targets, test_preds) ** 0.5
    print(f"  test  mae {test_mae:.2f}F   rmse {test_rmse:.2f}F")

    suffix = target
    torch.save(model.state_dict(), MODELS_DIR / f"lstm_{suffix}.pt")
    np.save(MODELS_DIR / f"lstm_{suffix}_scaler_mean.npy",  scaler.mean_)
    np.save(MODELS_DIR / f"lstm_{suffix}_scaler_scale.npy", scaler.scale_)
    pd.DataFrame({"pred": val_preds, "actual": val_targets},
                 index=val_index).to_csv(MODELS_DIR / f"lstm_{suffix}_val_preds.csv")
    pd.DataFrame({"pred": test_preds, "actual": test_targets},
                 index=test_index).to_csv(MODELS_DIR / f"lstm_{suffix}_test_preds.csv")
    print("  saved: model + scaler + val/test predictions")

    return test_mae, test_rmse


if __name__ == "__main__":
    results = {}
    for target in ["target_temp_next_24h", "target_daily_high_next_day"]:
        mae, rmse = run_lstm_pipeline(target)
        results[target] = {"mae": mae, "rmse": rmse}

    print("\nlstm summary")
    for target, r in results.items():
        print(f"  {target}:  mae {r['mae']:.2f}F   rmse {r['rmse']:.2f}F")
    print("\nrun research/compare_models.py next to see xgboost vs lstm head-to-head.")
