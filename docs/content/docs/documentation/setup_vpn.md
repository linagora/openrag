---
title: 🌐 VPN Setup for Remote Machines with WireGuard
---

This guide helps you securely connect your remote machines using **WireGuard VPN**, allowing you to share files (NFS, etc.) as if they were on the same private network.

---

## 1️⃣ Install WireGuard on all machines

Run the following on **each machine** (server and clients):

```bash
sudo apt update
sudo apt install -y wireguard
```

---

## 2️⃣ Configure the VPN Server (Main machine `X.X.X.X`)

Create the configuration file:

```bash
sudo nano /etc/wireguard/wg0.conf
```

Paste the following:

```ini
// /etc/wireguard/wg0.conf
...
[Interface]
Address = 10.0.0.1/24
PrivateKey = <SERVER_PRIVATE_KEY>
ListenPort = 51820

# Allow forwarding and NAT
PostUp = sysctl -w net.ipv4.ip_forward=1
PostUp = iptables -A FORWARD -i wg0 -j ACCEPT
PostUp = iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE
PostDown = iptables -D FORWARD -i wg0 -j ACCEPT
PostDown = iptables -t nat -D POSTROUTING -o eth0 -j MASQUERADE

[Peer]
# Client machine
PublicKey = <CLIENT_PUBLIC_KEY>
AllowedIPs = 10.0.0.2/32
```

---

## 3️⃣ Configure the VPN Client (Other machine `X.X.X.X`)

Create the configuration file:

```bash
sudo nano /etc/wireguard/wg0.conf
```

Paste the following:

```ini
// /etc/wireguard/wg0.conf
...
[Interface]
Address = 10.0.0.2/24
PrivateKey = <CLIENT_PRIVATE_KEY>

[Peer]
# VPN Server
PublicKey = <SERVER_PUBLIC_KEY>
Endpoint = X.X.X.X:51820 # Replace with your VPN server IP
AllowedIPs = 10.0.0.0/24
PersistentKeepalive = 25
```

---

## 🔑 Generate Keys on Each Machine

On **each machine**, run:

```bash
wg genkey | tee privatekey | wg pubkey > publickey
```

Use the generated keys in your configurations:
- `privatekey` → `<PRIVATE_KEY>`
- `publickey` → to give to the peer

---

## 🚀 Start and Enable VPN on Both Machines

To start the VPN connection:
```bash
sudo wg-quick up wg0
```

To enable the VPN automatically on boot:
```bash
sudo systemctl enable wg-quick@wg0
```

---

## ✅ Verification

Test the VPN connection:
- From **client**:
  ```bash
  ping 10.0.0.1
  ```
- From **server**:
  ```bash
  ping 10.0.0.2
  ```

---

:::caution
- After the VPN is up, you can configure services like **NFS** using the **10.0.0.0/24 private network**.
- Make sure your firewall allows `UDP 51820`.
- Adjust the `AllowedIPs` and network according to your needs.
:::