#!/usr/bin/env bash
# Called by scripts/deploy_backend.py over an authenticated SSH session.
set -Eeuo pipefail
release=$1
expected_hash=$2
migrate=${3:-false}
[[ "$migrate" == true || "$migrate" == false ]]
[[ "$release" =~ ^backend-[0-9]{8}-[0-9]{6}$ ]]
[[ "$expected_hash" =~ ^[a-f0-9]{64}$ ]]
cd /opt/aigc-prompt
umask 077
exec 9> .backend-deploy.lock
flock -n 9 || { echo 'Another backend deployment is running'; exit 1; }
backup="releases/$release"
printf '%s  %s\n' "$expected_hash" "$backup/release.tar.gz" | sha256sum -c -
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
switched=true
sudo -n docker compose up -d --no-deps --no-build api
sudo -n docker compose exec -T gateway nginx -t
sudo -n docker compose exec -T gateway nginx -s reload
healthy=false
for attempt in {1..20}; do
    if curl --fail --silent --show-error --max-time 5 https://62.234.69.93:8443/health; then
        healthy=true
        break
    fi
    sleep 2
done
[[ "$healthy" == true ]]
success=true
sudo -n docker inspect aigc-prompt-api-1 --format '{{.Image}}' > "$backup/image.after"
echo "Backend deployed. Backup: /opt/aigc-prompt/$backup"
