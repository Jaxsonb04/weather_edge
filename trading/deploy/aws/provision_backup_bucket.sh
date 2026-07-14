#!/usr/bin/env bash
set -euo pipefail

# Operator checkpoint: this script requires an AWS identity allowed to manage
# S3 bucket controls and the EC2 instance role. It is intentionally separate
# from deployment so source sync never acquires infrastructure-admin authority.

BUCKET="${1:-}"
REGION="${2:-}"
INSTANCE_ID="${3:-}"
AWS_CLI="${AWS_CLI:-aws}"

if [[ -z "$BUCKET" || -z "$REGION" || -z "$INSTANCE_ID" ]]; then
  echo "usage: $0 BUCKET REGION INSTANCE_ID" >&2
  exit 2
fi
if [[ ! "$BUCKET" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]]; then
  echo "invalid S3 bucket name: $BUCKET" >&2
  exit 1
fi
command -v "$AWS_CLI" >/dev/null 2>&1 || {
  echo "AWS CLI is required" >&2
  exit 1
}
"$AWS_CLI" sts get-caller-identity >/dev/null

if ! "$AWS_CLI" s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1; then
  if [[ "$REGION" == "us-east-1" ]]; then
    "$AWS_CLI" s3api create-bucket --bucket "$BUCKET" --region "$REGION" >/dev/null
  else
    "$AWS_CLI" s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
      --create-bucket-configuration "LocationConstraint=$REGION" >/dev/null
  fi
fi

"$AWS_CLI" s3api put-public-access-block --bucket "$BUCKET" \
  --public-access-block-configuration \
  'BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true'
"$AWS_CLI" s3api put-bucket-versioning --bucket "$BUCKET" \
  --versioning-configuration Status=Enabled
"$AWS_CLI" s3api put-bucket-encryption --bucket "$BUCKET" \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
"$AWS_CLI" s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" \
  --lifecycle-configuration \
  '{"Rules":[{"ID":"paper-journal-retention","Status":"Enabled","Filter":{"Prefix":"paper_trading/"},"Expiration":{"Days":90},"NoncurrentVersionExpiration":{"NoncurrentDays":30}},{"ID":"database-backup-retention","Status":"Enabled","Filter":{"Prefix":"database-snapshots/"},"Expiration":{"Days":35},"NoncurrentVersionExpiration":{"NoncurrentDays":7}}]}'

profile_arn="$("$AWS_CLI" ec2 describe-instances --region "$REGION" \
  --instance-ids "$INSTANCE_ID" \
  --query 'Reservations[0].Instances[0].IamInstanceProfile.Arn' --output text)"
if [[ -z "$profile_arn" || "$profile_arn" == "None" ]]; then
  echo "EC2 instance has no IAM instance profile: $INSTANCE_ID" >&2
  exit 1
fi
profile_name="${profile_arn##*/}"
role_name="$("$AWS_CLI" iam get-instance-profile --instance-profile-name "$profile_name" \
  --query 'InstanceProfile.Roles[0].RoleName' --output text)"
if [[ -z "$role_name" || "$role_name" == "None" ]]; then
  echo "instance profile has no IAM role: $profile_name" >&2
  exit 1
fi

policy="$(printf '{"Version":"2012-10-17","Statement":[{"Sid":"BucketLocation","Effect":"Allow","Action":"s3:GetBucketLocation","Resource":"arn:aws:s3:::%s"},{"Sid":"PrefixListing","Effect":"Allow","Action":"s3:ListBucket","Resource":"arn:aws:s3:::%s","Condition":{"StringLike":{"s3:prefix":["paper_trading/*","database-snapshots/*"]}}},{"Sid":"WeatherEdgeBackupObjects","Effect":"Allow","Action":["s3:PutObject","s3:GetObject","s3:AbortMultipartUpload","s3:ListMultipartUploadParts"],"Resource":["arn:aws:s3:::%s/paper_trading/*","arn:aws:s3:::%s/database-snapshots/*"]}]}' "$BUCKET" "$BUCKET" "$BUCKET" "$BUCKET")"
"$AWS_CLI" iam put-role-policy --role-name "$role_name" \
  --policy-name WeatherEdgePaperBackupAccess --policy-document "$policy"

[[ "$("$AWS_CLI" s3api get-bucket-versioning --bucket "$BUCKET" --query Status --output text)" == "Enabled" ]]
"$AWS_CLI" s3api get-public-access-block --bucket "$BUCKET" >/dev/null
"$AWS_CLI" s3api get-bucket-encryption --bucket "$BUCKET" >/dev/null
"$AWS_CLI" s3api get-bucket-lifecycle-configuration --bucket "$BUCKET" >/dev/null

echo "backup infrastructure ready"
echo "SFO_ARCHIVE_S3_BUCKET=$BUCKET"
echo "SFO_ARCHIVE_S3_PREFIX=paper_trading"
echo "SFO_DATABASE_BACKUP_S3_PREFIX=database-snapshots"
