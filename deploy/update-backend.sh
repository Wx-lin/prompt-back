#!/usr/bin/env bash
# Called by scripts/deploy_backend.py over an authenticated SSH session.
set -Eeuo pipefail
release=$1
expected_hash=$2
migrate=${3:-false}
admin_hash=${4:-}
auto_migration=${5:-}
[[ -z "$admin_hash" || "$admin_hash" =~ ^[a-f0-9]{64}$ ]]
[[ -z "$auto_migration" || "$auto_migration" == e7a13c9b40d2 ]]
[[ "$migrate" == true || "$migrate" == false ]]
[[ "$release" =~ ^backend-[0-9]{8}-[0-9]{6}$ ]]
[[ "$expected_hash" =~ ^[a-f0-9]{64}$ ]]
cd /opt/aigc-prompt
umask 077
exec 8> .release-deploy.lock
flock -w 600 8 || { echo 'Timed out waiting for another production deployment'; exit 1; }
exec 9> .backend-deploy.lock
flock -w 600 9 || { echo 'Timed out waiting for another backend deployment'; exit 1; }
backup="releases/$release"
printf '%s  %s\n' "$expected_hash" "$backup/release.tar.gz" | sha256sum -c -
if [[ -n "$admin_hash" ]]; then
    printf '%s  %s\n' "$admin_hash" "$backup/admin.tar.gz" | sha256sum -c -
    # Reject paths, links and special files before extracting the verified static bundle.
    python3 - "$backup/admin.tar.gz" <<'PYTHON'
import sys, tarfile
from pathlib import PurePosixPath
with tarfile.open(sys.argv[1]) as archive:
    for item in archive.getmembers():
        path = PurePosixPath(item.name)
        if path.is_absolute() or ".." in path.parts or not (item.isfile() or item.isdir()):
            raise SystemExit("Unsafe admin archive member")
PYTHON
fi
sudo -n docker compose config --quiet
available=$(df -Pk . | awk 'NR==2 {print $4}')
(( available >= 1048576 )) || { echo 'Less than 1 GiB free; deployment stopped'; exit 1; }
# Preserve the original timer state; never start a previously inactive timer.
timer_was_active=false
if sudo -n systemctl is-active --quiet aigc-crawl.timer; then
    timer_was_active=true
    sudo -n systemctl stop aigc-crawl.timer
fi
switched=false
success=false
admin_switched=false
old_id=
old_reference=
finish() {
    code=$?
    trap - EXIT
    set +e
    if [[ "$success" != true && -n "$old_id" ]]; then
        sudo -n docker tag "$old_id" "$old_reference" || code=1
    fi
    if [[ "$success" != true ]]; then
        for dir in app scripts alembic deploy; do
            if [[ -d "$backup/$dir.previous" ]]; then
                if [[ -e "backend/$dir" ]]; then
                    mv "backend/$dir" "$backup/$dir.failed"
                fi
                mv "$backup/$dir.previous" "backend/$dir" || code=1
            fi
        done
        if [[ -d "$backup/backend.before" ]]; then
            for file in alembic.ini pyproject.toml uv.lock; do
                cp "$backup/backend.before/$file" "backend/$file" || code=1
            done
        fi
    fi
    if [[ "$admin_switched" == true && "$success" != true ]]; then
        sudo -n install -m 0644 "$backup/admin.index.previous" backend/admin-panel/dist/index.html.rollback || code=1
        sudo -n mv backend/admin-panel/dist/index.html.rollback backend/admin-panel/dist/index.html || code=1
    fi
    if [[ "$switched" == true && "$success" != true ]]; then
        echo 'Health check failed. Restoring previous backend image.'
        sudo -n docker compose -f compose.yml -f "$backup/rollback.yml" up -d --no-deps --no-build api
        sudo -n docker compose exec -T gateway nginx -s reload
    fi
    if [[ "$timer_was_active" == true ]]; then
        sudo -n systemctl start aigc-crawl.timer || code=1
    fi
    exit "$code"
}
trap finish EXIT
for attempt in {1..60}; do
    if [[ -z $(sudo -n docker ps --filter name=aigc-scheduled-worker --format '{{.Names}}') ]]; then
        break
    fi
    echo 'Waiting for scheduled worker to finish...'
    sleep 5
done
if [[ -n $(sudo -n docker ps --filter name=aigc-scheduled-worker --format '{{.Names}}') ]]; then
    echo 'Scheduled worker is still active; retry later.'
    exit 1
fi
old_reference=$(sudo -n docker compose config --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["services"]["api"]["image"])')
old_id=$(sudo -n docker inspect aigc-prompt-api-1 --format '{{.Image}}')
sudo -n docker tag "$old_id" "aigc-prompt-app:rollback-$release"
printf 'services:\n  api:\n    image: aigc-prompt-app:rollback-%s\n  worker:\n    image: aigc-prompt-app:rollback-%s\n' "$release" "$release" > "$backup/rollback.yml"
cp compose.yml "$backup/compose.before.yml"
cp -a backend "$backup/backend.before"
sudo -n docker compose exec -T postgres pg_dump -U aigc -d aigc_prompt -Fc > "$backup/database.dump"
test -s "$backup/database.dump"
sudo -n docker compose exec -T postgres pg_restore --list < "$backup/database.dump" > "$backup/database.contents"
sudo -n docker compose exec -T api alembic current > "$backup/revision.before"
tar -xzf "$backup/release.tar.gz" -C "$backup"
# Replace only source directories; leave admin artifacts and all runtime state intact.
for dir in app scripts alembic deploy; do
    mv "backend/$dir" "$backup/$dir.previous"
    cp -a "$backup/backend/$dir" "backend/$dir"
done
# Backend-only deploy preserves the server gateway configuration.
if [[ -f "$backup/deploy.previous/nginx.conf" ]]; then
    cp "$backup/deploy.previous/nginx.conf" backend/deploy/nginx.conf
fi
for file in alembic.ini pyproject.toml uv.lock; do
    cp "$backup/backend/$file" "backend/$file"
done
dependencies_same=true
for file in pyproject.toml uv.lock deploy/requirements.txt deploy/Dockerfile deploy/patch_crawler.py deploy/start-app.sh deploy/chromium-wrapper; do
    if ! cmp -s "backend/$file" "$backup/backend.before/$file"; then
        dependencies_same=false
    fi
done
if [[ "$dependencies_same" == true ]]; then
    # Reuse the verified dependency layer; avoid copying/chowning 2GB of dependencies.
    cat > "$backup/Incremental.Dockerfile" <<DOCKERFILE
FROM aigc-prompt-app:rollback-$release
USER root
RUN rm -rf /app/app /app/scripts /app/alembic
COPY --chown=app:app backend/app /app/app
COPY --chown=app:app backend/scripts /app/scripts
COPY --chown=app:app backend/alembic /app/alembic
COPY --chown=app:app backend/alembic.ini /app/alembic.ini
USER app
DOCKERFILE
    mkdir -p "$backup/build-context/backend"
    cp -a "$backup/backend/app" "$backup/backend/scripts" "$backup/backend/alembic" "$backup/backend/alembic.ini" "$backup/build-context/backend/"
    sudo -n docker build -f "$backup/Incremental.Dockerfile" -t "$old_reference" "$backup/build-context" > "$backup/build.log" 2>&1 || {
        tail -40 "$backup/build.log"; exit 1;
    }
else
    available=$(df -Pk . | awk 'NR==2 {print $4}')
    (( available >= 5242880 )) || { echo 'Dependency rebuild needs at least 5 GiB free'; exit 1; }
    sudo -n docker compose build api > "$backup/build.log" 2>&1 || { tail -40 "$backup/build.log"; exit 1; }
fi
sudo -n docker compose run --rm --no-deps -T api alembic current > "$backup/revision.after"
sudo -n docker compose run --rm --no-deps -T api alembic heads > "$backup/heads"
current=$(awk '/^[0-9a-f]+([[:space:]]|$)/ {print $1}' "$backup/revision.after")
heads=$(awk '/\(head\)/ {print $1}' "$backup/heads")
[[ "$heads" =~ ^[0-9a-f]+$ ]] || { echo 'Expected one migration head; deployment stopped'; exit 1; }
if [[ "$current" != "$heads" ]]; then
    if [[ "$migrate" != true && "$auto_migration" == e7a13c9b40d2 && "$current" == 5c8e9f0a12b3 && "$heads" == e7a13c9b40d2 ]]; then
        migrate=true
    fi
    if [[ "$migrate" != true ]]; then
        echo "Database migration required (${current:-base} -> $heads). Backend not switched."
        echo 'Review backward compatibility, then rerun deploy_backend.py --migrate.'
        exit 1
    fi
    echo "Applying reviewed migration (${current:-base} -> $heads) with the new image."
    # Database backup is already validated. Never migrate with the old running API image.
    sudo -n docker compose run --rm --no-deps -T api alembic upgrade head > "$backup/migration.log" 2>&1 || {
        tail -40 "$backup/migration.log"
        echo 'Migration failed. Backend not switched; database is not automatically downgraded.'
        exit 1
    }
    sudo -n docker compose run --rm --no-deps -T api alembic current > "$backup/revision.after"
    current=$(awk '/^[0-9a-f]+([[:space:]]|$)/ {print $1}' "$backup/revision.after")
fi
[[ "$current" == "$heads" ]] || { echo 'Migration did not reach the target head; backend not switched'; exit 1; }
if [[ -n "$admin_hash" ]]; then
    mkdir "$backup/admin.new"
    tar -xzf "$backup/admin.tar.gz" -C "$backup/admin.new"
    test -s "$backup/admin.new/index.html"
    test -d backend/admin-panel/dist
    cp backend/admin-panel/dist/index.html "$backup/admin.index.previous"
    # Keep old hashed assets for open browser tabs and rollback; publish index last.
    if [[ -d "$backup/admin.new/assets" ]]; then
        sudo -n mkdir -p backend/admin-panel/dist/assets
        sudo -n cp -a "$backup/admin.new/assets/." backend/admin-panel/dist/assets/
    fi
    for file in "$backup/admin.new"/*; do
        [[ "$(basename "$file")" == index.html || "$(basename "$file")" == assets ]] && continue
        sudo -n cp -a "$file" backend/admin-panel/dist/
    done
    sudo -n install -m 0644 "$backup/admin.new/index.html" backend/admin-panel/dist/index.html.next
    # umask 077 protects backups, but nginx must read the public static files.
    sudo -n find backend/admin-panel/dist -type d -exec chmod 0755 {} +
    sudo -n find backend/admin-panel/dist -type f -exec chmod 0644 {} +
    admin_switched=true
    sudo -n mv backend/admin-panel/dist/index.html.next backend/admin-panel/dist/index.html
fi
switched=true
sudo -n docker compose up -d --no-deps --no-build api
sudo -n docker compose exec -T gateway nginx -t
sudo -n docker compose exec -T gateway nginx -s reload
admin_healthy() {
    [[ -n "$admin_hash" ]] || return 0
    curl --fail --silent --show-error --max-time 5 https://62.234.69.93:8443/admin/ \
        --output "$backup/admin.public.html" || return 1
    [[ "$(sha256sum "$backup/admin.public.html" | awk '{print $1}')" == \
       "$(sha256sum "$backup/admin.new/index.html" | awk '{print $1}')" ]]
}
healthy=false
for attempt in {1..20}; do
    if curl --fail --silent --show-error --max-time 5 https://62.234.69.93:8443/health && admin_healthy; then
        healthy=true
        break
    fi
    sleep 2
done
[[ "$healthy" == true ]]
success=true
sudo -n docker inspect aigc-prompt-api-1 --format '{{.Image}}' > "$backup/image.after"
echo "Backend deployed. Backup: /opt/aigc-prompt/$backup"
