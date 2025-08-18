# 🪵 GlusterFS Setup for Shared Storage (Ray Cluster)

In a Ray distributed setup, **all worker nodes need access to certain shared resources** used by the application.  
This includes:

- `.env` (environment variables for models and settings)
- `.hydra_config` (application configuration)
- Uploaded files (`/data`)
- Model weights (e.g. `/model_weights` if using HF local cache)

---

## 1️⃣ Setup VPN (if required)

If your Ray nodes are **not on the same local network**, set up a VPN between them first.  
➡ Refer to the dedicated [VPN setup guide](../docs/setup_vpn.md).  
You can skip this step if your nodes are already on the same LAN.

---

## 2️⃣ Setup GlusterFS (Distributed Filesystem)

GlusterFS allows you to **share and replicate storage across multiple nodes** with redundancy and better fault tolerance.

This guide assumes:
- You have 4 machines on the same private network
- You want all of them to share `/ray_mount`

---

### 🔧 Install GlusterFS

Run this on **all 4 machines**:

```bash
sudo apt update
sudo apt install -y glusterfs-server
sudo systemctl enable --now glusterd
```

---

### 🤝 Connect all nodes into a trusted pool

From one node (e.g. the Ray head), run:

```bash
gluster peer probe <IP_OF_NODE_2>
gluster peer probe <IP_OF_NODE_3>
gluster peer probe <IP_OF_NODE_4>
```

Confirm with:

```bash
gluster peer status
```

---

### 📁 Create bricks on each node

On **each node**, run:

```bash
sudo mkdir -p /gluster/bricks/ray_mount
```

---

### 📦 Create the replicated GlusterFS volume

From one node (e.g. the Ray head):

```bash
gluster volume create rayvol replica 4 \
  <IP1>:/gluster/bricks/ray_mount \
  <IP2>:/gluster/bricks/ray_mount \
  <IP3>:/gluster/bricks/ray_mount \
  <IP4>:/gluster/bricks/ray_mount \
  force
```

Start the volume:

```bash
gluster volume start rayvol
```

---

### 🔗 Mount the volume on all nodes

Install the client tools:

```bash
sudo apt install -y glusterfs-client
```

Create the mount point:

```bash
sudo mkdir -p /ray_mount
```

Mount it (on each node):

```bash
sudo mount -t glusterfs <ANY_NODE_IP>:/rayvol /ray_mount
```

To make this permanent across reboots:

```bash
echo "<ANY_NODE_IP>:/rayvol /ray_mount glusterfs defaults,_netdev 0 0" | sudo tee -a /etc/fstab
```

> ✅ Replace `<ANY_NODE_IP>` with one of your node IPs in the GlusterFS cluster.

---

### 📂 Copy required data to the shared folder

From any node:

```bash
sudo cp -r .hydra_config /ray_mount/
sudo cp .env /ray_mount/
sudo mkdir /ray_mount/data /ray_mount/model_weights
sudo chown -R ubuntu:ubuntu /ray_mount
```

> ✅ Ensure that the ownership is set to the user running Ray workers (e.g. `ubuntu`) so that all nodes can read/write.

---

Now, all Ray nodes will have **consistent access to required data and configurations** via `/ray_mount`, backed by a fault-tolerant and distributed filesystem.