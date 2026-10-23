# Rolling out a new build

How a plugin update gets from upstream to the production workers, and what has to be set up once.
Background: hb-backend#2573 (the cursor fix), hb-backend#2602 (pins and `stage-bucket.sh`).

## The flow

| Step | Where | Trigger | Writes |
|---|---|---|---|
| 1. Update check | this repo, `huntbase-update-check.yml` | weekly (Mon 06:17 UTC) or Run workflow | one rolling PR from `bot/plugin-updates`: `plugins.txt` + `BUILD_VERSION` |
| 2. Build, smoke test, release | this repo, `huntbase-release.yml` | merging that PR (`BUILD_VERSION` changed), then **an admin publishes the draft** | release `huntbase-rN` |
| 3. Stage staging | hb-backend, `steampipe-rollout.yml` | hourly check for a new release | `gs://huntbase-staging-shared-libs/steampipe-sqlite-huntbase-rN/` |
| 4. PRs | hb-backend, `steampipe-rollout.yml` | same run | hb-backend PR (pins), helm-charts PR (`values-staging.yaml`) |
| 5. Production | hb-backend, `steampipe-rollout.yml` | **Run workflow, target `production`, plus approval** | `gs://huntbase-production-shared-libs/...`, helm-charts PR (`values-production.yaml`) |

People merge every PR. No workflow merges anything or deploys by itself.

1. **Update check.** `huntbase/update_check.py` looks up the newest stable release of every plugin in
   `plugins.txt` (`turbot/steampipe-plugin-<name>`, or `<org>/steampipe-plugin-<name>` such as
   `huntbase-io/anthropic`). If any are newer, it bumps them and `BUILD_VERSION` (to one past the highest
   `huntbase-rN`) and opens or refreshes **one** PR with a table of old → new versions, release notes,
   SDK major (v5 → v6 flagged), and removed tables. It also reports upstream `turbot/steampipe-sqlite`
   commits we lack and whether upstream now has its own fresh-stream-per-Filter fix. That is a warning on
   the run; if it lands, consider dropping our cursor fix. No updates means no PR (an open one is closed). Because GITHUB_TOKEN pushes
   don't start workflows, it starts the release build on the PR branch with a dispatch (no publish).
2. **Release.** A push to `dev` that changes `BUILD_VERSION` builds every plugin, then
   `huntbase/smoke_test.py` checks the exact archives: `SHA256SUMS`, the manifest against `plugins.txt`,
   every extension loads and `huntbase_build_<plugin>()` reports this build with `refilter`, and IN-list
   and join queries on the terraform extension (local `.tf` files, no network) return every row. Only
   then is `huntbase-rN` created, with the smoke-test report in its notes and as `smoke-test.json`.
   It is created as a **draft**: the "Protect release tags" ruleset lets only repository admins create
   `huntbase-r*` tags, and publishing a release creates its tag, so `GITHUB_TOKEN` can't publish it.
   An admin opens the draft (Releases → `huntbase-rN` → Edit) and clicks **Publish release**; drafts
   are invisible to the rollout until then. Alternatively an admin pushes the tag
   `huntbase-rN` on the merge commit, which rebuilds and publishes directly. Existing tags are never reused. Pushing a tag `huntbase-rN-<suffix>` makes a test prerelease, which
   the rollout ignores.
3. **Staging (hb-backend).** Within the hour, `steampipe-rollout.yml` sees a release newer than
   hb-backend's `HUNTBASE_RELEASE`, re-runs the smoke test on the published assets (no credentials in that job), then
   runs `utils/steampipe/stage-bucket.sh` against `huntbase-staging-shared-libs`. It never overwrites a
   folder: an existing complete one is reused, a partial one fails the run.
4. **PRs.** hb-backend → `dev`: `utils/steampipe/pin-release.py huntbase-rN` rewrites `HUNTBASE_RELEASE`,
   `plugins.txt` and `plugins.sha256` from the release's `manifest.json` and `SHA256SUMS`, so local and
   app.ldn installs match. helm-charts → `staging`: `EXTENSIONS_DIR: /extensions/steampipe-sqlite-huntbase-rN`
   in `charts/backend/values-staging.yaml`. It is opened only once the folder exists. Merging it is the staging deploy (ArgoCD).
5. **Production.** In hb-backend, Actions → *Steampipe extensions rollout* → Run workflow, release
   `huntbase-rN`, target `production`. It refuses unless staging's `EXTENSIONS_DIR` is already that
   release. The stage job then waits for a reviewer on the `steampipe-production` environment, stages
   `huntbase-production-shared-libs`, and opens the helm-charts PR for `values-production.yaml` (on
   `staging`, promoted to `production` as usual).

Rollback at any point: revert the helm-charts PR. Old folders and the bucket root are never modified.

## Why the rollout runs in hb-backend

This repo is public. The org's GCP and GitHub App secrets (`GCP_WORKLOAD_IDENTITY_PROVIDER`,
`HELM_CHARTS_APP_*`) are only shared with private repos, and a key that can write helm-charts should not
sit in a public repo. hb-backend already holds them, owns `stage-bucket.sh` and the pins, and opens
helm-charts PRs the same way in `deploy.yml`. This repo needs nothing but its own `GITHUB_TOKEN`.

## One-time setup

### GCP (Tyler; nothing here has been applied)

Reuse the org's GitHub OIDC provider:
`projects/258707721010/locations/global/workloadIdentityPools/github-pool/providers/github-provider`
(project `huntbase-tools`, condition `assertion.repository_owner == 'huntbase-io'`). Do **not** reuse
`gh-actions-gar-publisher`, which any org repo can impersonate. Use one service account per environment,
which only hb-backend jobs in the matching GitHub environment can impersonate. The
helm-charts PR for `tf/gcp/environments/{staging,production}` contains the Terraform. By hand it is:

```bash
POOL=projects/258707721010/locations/global/workloadIdentityPools/github-pool
for ENV in staging production; do
  PROJECT=huntbase-$ENV
  SA=steampipe-stager@$PROJECT.iam.gserviceaccount.com
  gcloud iam service-accounts create steampipe-stager --project $PROJECT \
    --display-name "Stages steampipe-sqlite releases (hb-backend steampipe-rollout)"
  # Create + read on that one bucket. No delete, so it can't overwrite or remove anything.
  for ROLE in roles/storage.objectCreator roles/storage.objectViewer; do
    gcloud storage buckets add-iam-policy-binding gs://huntbase-$ENV-shared-libs \
      --member serviceAccount:$SA --role $ROLE
  done
  # Only hb-backend jobs running in the steampipe-<env> GitHub environment.
  gcloud iam service-accounts add-iam-policy-binding $SA --project $PROJECT \
    --role roles/iam.workloadIdentityUser \
    --member "principal://iam.googleapis.com/$POOL/subject/repo:huntbase-io/hb-backend:environment:steampipe-$ENV"
done
```

The `principal://.../subject/...` member needs the provider to map `google.subject=assertion.sub` (the
standard GitHub mapping). Check with
`gcloud iam workload-identity-pools providers describe github-provider --workload-identity-pool github-pool --location global --project huntbase-tools --format 'value(attributeMapping)'`.
If it isn't mapped, use
`principalSet://iam.googleapis.com/$POOL/attribute.repository/huntbase-io/hb-backend` instead. That is
weaker: any hb-backend workflow could then stage, and production is gated only by the GitHub environment.

### GitHub (Tyler)

In **hb-backend**, Settings → Environments:

| Environment | Protection | Variable `GCP_STEAMPIPE_SERVICE_ACCOUNT` |
|---|---|---|
| `steampipe-staging` | deployment branches: `dev` | `steampipe-stager@huntbase-staging.iam.gserviceaccount.com` |
| `steampipe-production` | deployment branches: `dev`; **required reviewers** (Tyler, Rakesh); no self-review bypass | `steampipe-stager@huntbase-production.iam.gserviceaccount.com` |

```bash
gh api -X PUT repos/huntbase-io/hb-backend/environments/steampipe-staging \
  -F 'deployment_branch_policy[protected_branches]=false' -F 'deployment_branch_policy[custom_branch_policies]=true'
gh api -X POST repos/huntbase-io/hb-backend/environments/steampipe-staging/deployment-branch-policies -f name=dev
gh variable set GCP_STEAMPIPE_SERVICE_ACCOUNT -R huntbase-io/hb-backend -e steampipe-staging \
  --body steampipe-stager@huntbase-staging.iam.gserviceaccount.com
# steampipe-production: same, plus reviewers (user ids from `gh api users/<login> -q .id`):
#   -F 'reviewers[][type]=User' -F 'reviewers[][id]=<id>' -F prevent_self_review=false
```

No new secrets. The rollout reuses the org secrets `GCP_WORKLOAD_IDENTITY_PROVIDER`,
`HELM_CHARTS_APP_ID` and `HELM_CHARTS_APP_PRIVATE_KEY`. Optional: add **hb-backend** to that GitHub App's
installation (with Contents and Pull requests write) so the hb-backend pin PR runs CI. Without it the PR is opened with
`GITHUB_TOKEN`, which works but doesn't trigger hb-backend's PR checks (none are required on `dev`).

In **this repo**, nothing to configure. The update check uses `GITHUB_TOKEN` (repo setting "Allow
GitHub Actions to create pull requests" is already on).

Until the GCP part exists, the hb-backend rollout still smoke-tests, opens the hb-backend pin PR and
dry-runs the staging step. It reports "Bucket not staged (no GCP credentials configured yet)" and opens no
helm-charts PR. Once the GCP part is set up, Run workflow for that release (target `staging`) to finish it.
