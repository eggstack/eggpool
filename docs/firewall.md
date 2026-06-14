# Firewall Configuration

The proxy listens on port 8080 by default. Restrict access to your LAN.

## nftables (recommended)

```bash
# Allow established connections
nft add rule inet filter input ct state established,related accept

# Allow loopback
nft add rule inet filter input iif lo accept

# Allow LAN access to port 8080
nft add rule inet filter input tcp dport 8080 ip saddr 192.168.0.0/16 accept
nft add rule inet filter input tcp dport 8080 ip saddr 10.0.0.0/8 accept
nft add rule inet filter input tcp dport 8080 ip saddr 172.16.0.0/12 accept

# Drop all other traffic to port 8080
nft add rule inet filter input tcp dport 8080 drop
```

To persist, save rules:

```bash
sudo nft list ruleset > /etc/nftables.conf
sudo systemctl enable nftables
```

## iptables

```bash
# Allow LAN access to port 8080
sudo iptables -A INPUT -p tcp --dport 8080 -s 192.168.0.0/16 -j ACCEPT
sudo iptables -A INPUT -p tcp --dport 8080 -s 10.0.0.0/8 -j ACCEPT
sudo iptables -A INPUT -p tcp --dport 8080 -s 172.16.0.0/12 -j ACCEPT

# Drop all other traffic to port 8080
sudo iptables -A INPUT -p tcp --dport 8080 -j DROP

# Save rules
sudo apt install iptables-persistent
sudo netfilter-persistent save
```

## ufw (Ubuntu)

```bash
# Allow from LAN only
sudo ufw allow from 192.168.0.0/16 to any port 8080
sudo ufw allow from 10.0.0.0/8 to any port 8080
sudo ufw allow from 172.16.0.0/12 to any port 8080

# Enable firewall
sudo ufw enable
```

## Binding to localhost only

If the proxy should only be accessible from the same machine, bind to localhost:

```toml
[server]
host = "127.0.0.1"
```

No firewall rules are needed in this case.

## Verify

```bash
# Check listening ports
ss -tlnp | grep 8080

# Test access from another machine
curl http://<proxy-ip>:8080/v1/healthz
```
