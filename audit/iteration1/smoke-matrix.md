# Iteration 1 smoke matrix

Status: `blocked_by_docker_storage`

The single allowed `docker info` check could not reach the Docker Desktop Linux engine. Task-image and probe-image checks were therefore not run, no image pull was attempted, and neither replay nor the real container isolation proof was executed.

| instance_id | mode | tests_executed | F2P (P/F/S/E) | P2P (P/F/S/E) | patch | cleanup | raw result SHA-256 | status | validity |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| django__django-11133 | noop | false | 0/0/0/0 | 0/0/0/0 | not_run | not_run | - | not_run | blocked |
| django__django-11133 | gold | false | 0/0/0/0 | 0/0/0/0 | not_run | not_run | - | not_run | blocked |
