# Verified Cloudflare publisher

The private controller builds and tests the application, verifies the deployable files, and retains its artifact. This repository contains the publisher and its synthetic tests. The publishing workflow downloads the verified archive into a temporary directory, checks its GitHub digest and deployment bindings, and emits only fixed status messages. It does not upload workflow artifacts.

Protect `main` with required pull requests and the `test` status check. Restrict the `production` environment to the protected `main` branch before supplying any secret. Store `SOURCE_REPOSITORY`, `SOURCE_WORKFLOW_SHA`, `WORKER_NAME`, and `CLOUDFLARE_ACCOUNT_ID` as environment secrets so the public runner masks their values. Keep the approved private controller commit pinned until the next reviewed revision.

`HANDOFF_ACTIONS_READ` grants Actions read access only to the private controller. Store it, `WORKER_DATABASE_ID`, and a Worker-scoped `CLOUDFLARE_API_TOKEN` in the protected environment. The verification-only run uses the read key and binding identifier. The Cloudflare key is passed only to the explicit deploy step. Run verification using `publish=false` before granting the Cloudflare key.

For isolated validation, run the same workflow from protected `main` with `isolated=true` and `publish=false`. Use a protected `handoff-validation` environment restricted to `main`. Supply the same source metadata and Actions read secret in that environment, plus `SOURCE_WORKFLOW_BRANCH` for the approved isolated controller branch. The Worker and D1 settings must be fixtures. The isolated job has no Cloudflare credentials and cannot deploy. Its ZIP and unpacked data each have a 32 MiB limit.
