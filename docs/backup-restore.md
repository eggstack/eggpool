# Backup and Restore

## What to Back Up

| File | Description | Frequency |
|------|-------------|-----------|
| `/etc/gorouter/config.toml` | Configuration | On change |
| `/etc/gorouter/env` | API keys | On change |
| `/var/lib/gorouter/usage.sqlite3*` | Database | Daily |

## Backup

### Manual backup

```bash
# Create backup directory
BACKUP_DIR="/var/backups/gorouter/$(date +%Y%m%d-%H%M%S)"
sudo mkdir -p "$BACKUP_DIR"

# Backup configuration
sudo cp /etc/gorouter/config.toml "$BACKUP_DIR/"
sudo cp /etc/gorouter/env "$BACKUP_DIR/"

# Backup database (using SQLite backup for consistency)
sudo -u gorouter sqlite3 /var/lib/gorouter/usage.sqlite3 ".backup '$BACKUP_DIR/usage.sqlite3'"

# Create archive
sudo tar czf "$BACKUP_DIR.tar.gz" -C /var/backups/gorouter "$(date +%Y%m%d-%H%M%S)"
sudo rm -rf "$BACKUP_DIR"

echo "Backup saved to $BACKUP_DIR.tar.gz"
```

### Automated backup (cron)

Create `/etc/cron.d/gorouter-backup`:

```
# Backup gorouter database daily at 2 AM
0 2 * * * root /usr/local/bin/gorouter-backup
```

Create `/usr/local/bin/gorouter-backup`:

```bash
#!/bin/bash
set -euo pipefail

BACKUP_DIR="/var/backups/gorouter"
KEEP_DAYS=30

mkdir -p "$BACKUP_DIR"

BACKUP_NAME="gorouter-$(date +%Y%m%d-%H%M%S)"
BACKUP_PATH="$BACKUP_DIR/$BACKUP_NAME"

mkdir -p "$BACKUP_PATH"

# Backup config
cp /etc/gorouter/config.toml "$BACKUP_PATH/"
cp /etc/gorouter/env "$BACKUP_PATH/"

# Backup database
sudo -u gorouter sqlite3 /var/lib/gorouter/usage.sqlite3 ".backup '$BACKUP_PATH/usage.sqlite3'"

# Archive
tar czf "$BACKUP_DIR/$BACKUP_NAME.tar.gz" -C "$BACKUP_DIR" "$BACKUP_NAME"
rm -rf "$BACKUP_PATH"

# Cleanup old backups
find "$BACKUP_DIR" -name "gorouter-*.tar.gz" -mtime +$KEEP_DAYS -delete
```

Make it executable:

```bash
sudo chmod +x /usr/local/bin/gorouter-backup
```

## Restore

### Stop the service

```bash
sudo systemctl stop gorouter
```

### Restore configuration

```bash
sudo cp backup/config.toml /etc/gorouter/config.toml
sudo cp backup/env /etc/gorouter/env
sudo chown root:gorouter /etc/gorouter/config.toml /etc/gorouter/env
sudo chmod 640 /etc/gorouter/config.toml /etc/gorouter/env
```

### Restore database

```bash
# Remove current database
sudo rm -f /var/lib/gorouter/usage.sqlite3*

# Restore from backup
sudo cp backup/usage.sqlite3 /var/lib/gorouter/usage.sqlite3
sudo chown gorouter:gorouter /var/lib/gorouter/usage.sqlite3
```

### Start the service

```bash
sudo systemctl start gorouter
```

### Verify

```bash
# Check service status
sudo systemctl status gorouter

# Check health
curl -s http://localhost:8080/v1/healthz

# Check dashboard
curl -s http://localhost:8080/
```

## Database Migration

After restoring a backup from an older version, run migrations:

```bash
sudo systemctl stop gorouter
sudo -u gorouter /opt/gorouter/.venv/bin/go-aggregator migrate --config /etc/gorouter/config.toml
sudo systemctl start gorouter
```

Migrations are idempotent and safe to run multiple times.
