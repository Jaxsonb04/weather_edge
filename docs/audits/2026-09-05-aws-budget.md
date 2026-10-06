# AWS budget assessment — September 5, 2026

Target: $28 per month for the AWS account.

## Change verified in AWS

The existing monthly cost budget was changed from $20 to **$28** and read back
from the AWS Budgets overview. Its existing notifications remain configured:

- actual spend above 85%: **$23.80**;
- forecasted spend above 100%: **$28.00**;
- actual spend above 100%: **$28.00**.

There are no budget actions. AWS Budgets reports delayed billing data and sends
notifications; it does not impose a hard account spending cap or stop services.

At the verification snapshot, September accrued spend was **$8.76** and AWS
forecast month-end spend was **$53.91**. The console home showed a nearby
$55.27 forecast. August closed at **$50.07** and July at **$33.73**. The $28
budget is therefore in place, but the running infrastructure is not yet sized
to stay below it.

## Current cost and resource evidence

September accrued spend by service at the snapshot was:

| Service | Accrued USD |
| --- | ---: |
| EC2 instances | 4.39 |
| S3 | 1.60 |
| Lightsail | 1.29 |
| EC2 other | 0.93 |
| VPC | 0.56 |
| **Total** | **8.76** |

The Lightsail console has no instances or snapshots. Its September amount is
consistent with part-month usage of a resource that has already been removed,
so it should not recur if the inventory remains empty. The retired east-region
EC2 inventory is also empty: no instances, volumes, snapshots, or Elastic IPs.

Northern California has one running production `t4g.medium`. It uses one
64 GiB gp3 volume at baseline gp3 performance and standard CPU credits. The
host snapshot showed 3,825 MiB RAM, approximately 3,381 MiB available at that
instant, and a latest successful archive/prune peak of 2.54 GiB. A direct 2 GiB
instance downgrade is therefore unsupported. Scheduler, scan, monitor,
archive/prune, and dataset checks sampled during this audit were healthy; this
was not a full production verification.

S3 is a material cost driver. Cost Explorer forecast **$12.20** of September S3
spend, with $1.55 already accrued as Standard storage. Account-authorized
inventory found:

- current versions: 1,246 objects and 237,307,086,150 bytes (about 221 GiB);
- noncurrent versions: 71 objects and 184,726,724,116 bytes (about 172 GiB);
- delete markers: 52.

The enabled lifecycle rules retain paper-journal objects for 90 days and their
noncurrent versions for 30 days. Database snapshots remain current for 35 days
and noncurrent for 7 days. The policy is functioning, but several full database
copies and recently deleted versions remain billable during those windows.

## What can reach $28

At current public Northern California rates, the recurring production floor is
approximately:

| Component | Monthly USD |
| --- | ---: |
| On-demand `t4g.medium` compute (730 hours) | 29.20 |
| 64 GiB gp3 | 6.14 |
| One public IPv4 address | 3.65 |
| S3 forecast | 12.20 |
| **Current recurring estimate** | **51.19** |

This agrees directionally with AWS's $53.91 account forecast. Current compute
alone exceeds the requested limit.

Keeping the 4 GiB host and reaching $28 requires both a compute commitment and
smaller backup storage. A three-year, no-upfront Standard Reserved Instance for
the existing size is approximately $12.63/month at the checked public rate.
Together with gp3 and IPv4, that leaves about $5.58 for S3, requests, and tax.
For a useful cushion, S3 should fall below about $3/month. A one-year reservation
does not reach the target, and the previously considered $24 Lightsail plan plus
the current S3 forecast would still exceed it.

A concrete operating target is:

1. Keep the latest 14 days of full database snapshots and expire their
   noncurrent versions after one day. At the observed backup cadence this would
   reduce full-snapshot storage sharply while retaining multiple verified,
   immediately restorable copies.
2. Preserve the separate 90-day paper-journal archive rule for now; those
   current objects occupy only about 3.5 GiB.
3. Add compression only after a downloaded object passes decompression,
   checksum, SQLite integrity, and foreign-key recovery tests.
4. After storage falls and is remeasured, buy the exact-size three-year
   no-upfront reservation only if the owner accepts the 36-month commitment.

Changing lifecycle retention deletes backup data, and buying a reservation is
a financial commitment. Neither was done during this audit. The $28 budget and
its notifications were the only account changes.

## Sources checked September 5

- [AWS Northern California EC2 price file](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current/us-west-1/index.csv)
- [AWS Northern California S3 price file](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonS3/current/us-west-1/index.csv)
- [AWS Budgets behavior](https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-managing-costs.html)
- [AWS S3 pricing](https://aws.amazon.com/s3/pricing/)

Billing, inventory, lifecycle, version, and budget values above came from the
authenticated AWS console and CloudShell. Host measurements came from the live
production machine. Sensitive identifiers and destinations are intentionally
omitted.
