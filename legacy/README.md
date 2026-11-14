# Legacy

Things from the March 2026 v2 build that the rebuilt engine does not use: the psutil metric agent, the CodeDeploy and systemd deploy files, and the old helper scripts. They are kept for history, nothing imports them and none of it is tested. The v2 API endpoints still in `backend/api/` are off unless `LEGACY_API_ENABLED=true`.

Paths inside these files still point at the old `/home/ec2-user/NeuroOps` layout.
