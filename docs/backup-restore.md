# Backup and Restore

## What to Back Up

| File | Description | Frequency |
|------|-------------|-----------|
| `/etc/eggpool/config.toml` | Configuration | On change |
| `/etc/eggpool/env` | API keys | On change |
| `/var/lib/eggpool/usage.sqlite3*` | Database | Daily |

## Backup

### Manual backup

```bash
# Create backup directory
BACKUP_DIR="/var/backups/eggpool/$(date +%Y%m%d-%H%M%S)"
sudo mkdir -p "$BACKUP_DIR"

# Backup configuration
sudo cp /etc/eggpool/config.toml "$BACKUP_DIR/"
sudo cp /etc/eggpool/env "$BACKUP_DIR/"

# Backup database (using SQLite backup for consistency)
sudo -u eggpool sqlite3 /var/lib/eggpool/usage.sqlite3 ".backup '$BACKUP_DIR/usage.sqlite3'"

# Create archive
sudo tar czf "$BACKUP_DIR.tar.gz" -C /var/backups/eggpool "$(date +%Y%m%d-%H%M%S)"
sudo rm -rf "$BACKUP_DIR"

echo "Backup saved to $BACKUP_DIR.tar.gz"
```

### Automated backup (cron)

Create `/etc/cron.d/eggpool-backup`:

```
# Backup eggpool database daily at 2 AM
0 2 * * * root /usr/local/bin/eggpool-backup
```

Create `/usr/local/bin/eggpool-backup`:

```bash
#!/bin/bash
set -euo pipefail

BACKUP_DIR="/var/backups/eggpool"
KEEP_DAYS=30

mkdir -p "$BACKUP_DIR"

BACKUP_NAME="eggpool-$(date +%Y%m%d-%H%M%S)"
BACKUP_PATH="$BACKUP_DIR/$BACKUP_NAME"

mkdir -p "$BACKUP_PATH"

# Backup config
cp /etc/eggpool/config.toml "$BACKUP_PATH/"
cp /etc/eggpool/env "$BACKUP_PATH/"

# Backup database
sudo -u eggpool sqlite3 /var/lib/eggpool/usage.sqlite3 ".backup '$BACKUP_PATH/usage.sqlite3'"

# Archive
tar czf "$BACKUP_DIR/$BACKUP_NAME.tar.gz" -C "$BACKUP_DIR" "$BACKUP_NAME"
rm -rf "$BACKUP_PATH"

# Cleanup old backups
find "$BACKUP_DIR" -name "eggpool-*.tar.gz" -mtime +$KEEP_DAYS -delete
```

Make it executable:

```bash
sudo chmod +x /usr/local/bin/eggpool-backup
```

## Restore

### Stop the service

```bash
sudo systemctl stop eggpool
```

### Restore configuration

```bash
sudo cp backup/config.toml /etc/eggpool/config.toml
sudo cp backup/env /etc/eggpool/env
sudo chown root:eggpool /etc/eggpool/config.toml /etc/eggpool/env
sudo chmod 640 /etc/eggpool/config.toml /etc/eggpool/env
```

### Restore database

```bash
# Remove current database
sudo rm -f /var/lib/eggpool/usage.sqlite3*

# Restore from backup
sudo cp backup/usage.sqlite3 /var/lib/eggpool/usage.sqlite3
sudo chown eggpool:eggpool /var/lib/eggpool/usage.sqlite3
```

### Start the service

```bash
sudo systemctl start eggpool
```

### Verify

```bash
# Check service status
sudo systemctl status eggpool

# Check health
curl -s http://localhost:8080/v1/healthz

# Check dashboard
curl -s http://localhost:8080/
```

## Database Migration

After restoring a backup from an older version, run migrations:

```bash
sudo systemctl stop eggpool
sudo -u eggpool /opt/eggpool/.venv/bin/eggpool --config /etc/eggpool/config.toml migrate
sudo systemctl start eggpool
```

Migrations are idempotent and safe to run multiple times.
