---
title: 🗄️ Data Model Overview
---
This document describes the database schema used for managing users, partitions (spaces), files, and their relationships.  
It is implemented using **SQLAlchemy ORM** with PostgreSQL as the backend.

---

## **Tables**

### 🧩 `users`
Stores information about API users and administrators.

| Column         | Type      | Description |
|----------------|-----------|-------------|
| `id`           | Integer (PK) | Unique user identifier |
| `external_user_id` | String (nullable, unique) | Optional external system reference |
| `display_name` | String | Display name |
| `token`        | String (unique, hashed) | SHA-256 hash of the user’s API token |
| `is_admin`     | Boolean | Marks system administrator users |
| `created_at`   | DateTime | Timestamp of creation |
| `file_quota`   | Integer (nullable) | Max files allowed for that user |
| `file_count`| Integer (default=0) | Number of uploaded files.|
**Relationships**
- `memberships`: one-to-many → `PartitionMembership`

---

### 📁 `partitions`
Represents a logical workspace or “space” that groups files and users.
:::caution
Note that "partition" has to be unique accross all app users as it is used as a partition key in Milvus.
:::

| Column       | Type | Description |
|---------------|------|-------------|
| `id`          | Integer (PK) | Unique partition identifier |
| `partition`   | String (unique, indexed) | Human-readable name / key |
| `created_at`  | DateTime | Timestamp of creation |

**Relationships**
- `files`: one-to-many → `File`
- `memberships`: one-to-many → `PartitionMembership`

---

### 📄 `files`
Represents an indexed file belonging to a partition.

| Column          | Type | Description |
|------------------|------|-------------|
| `id`             | Integer (PK) | Internal file identifier |
| `file_id`        | String (indexed) | External file identifier (e.g., hash or ID) |
| `partition_name` | String (FK → `partitions.partition`) | Partition that owns the file |
| `file_metadata`  | JSON | Additional metadata (format, size, etc.) |

**Constraints**
- `UniqueConstraint(file_id, partition_name)` → a file can appear only once per partition.  
- Composite index `ix_partition_file (partition_name, file_id)` for efficient queries.

---

### 👥 `partition_memberships`
Defines the many-to-many relationship between **users** and **partitions**, including role-based access control.

| Column          | Type | Description |
|------------------|------|-------------|
| `id`             | Integer (PK) | Unique row ID |
| `partition_name` | String (FK → `partitions.partition`, CASCADE) | Partition identifier |
| `user_id`        | Integer (FK → `users.id`, CASCADE) | Linked user |
| `role`           | String | Role of the user: `owner`, `editor`, or `viewer` |
| `added_at`       | DateTime | Timestamp of when the membership was created |

**Constraints**
- `UniqueConstraint(partition_name, user_id)` → a user can appear only once per partition.  
- `CheckConstraint(role IN ('owner','editor','viewer'))` → role validation.  
- Composite index `ix_user_partition (user_id, partition_name)`.

**Relationships**
- `partition`: many-to-one → `Partition`
- `user`: many-to-one → `User`

---

## **Relationships Summary**

| Relationship | Type | Description |
|---------------|------|-------------|
| `User` ↔ `PartitionMembership` | 1–N | A user can belong to multiple partitions with different roles |
| `Partition` ↔ `PartitionMembership` | 1–N | A partition can have multiple users (owners, editors, viewers) |
| `Partition` ↔ `File` | 1–N | A partition can contain multiple files |
| `File` ↔ `Partition` | N–1 | Each file belongs to exactly one partition |

---

## **Access Control**
- Roles (`owner`, `editor`, `viewer`) determine what users can do in each partition.  
- `is_admin` users are privileged globally (admin endpoints, user management).  
- `SUPER_ADMIN_MODE=true` allows the global admin to bypass all partition-level restrictions.  

---

## **File Quotas**
Limits the number of files a user can upload (indexed files + pending tasks).

- Admins always have unlimited quota and can update quota for a given user
- `DEFAULT_FILE_QUOTA <= 0` to disable quota checking. 
- `DEFAULT_FILE_QUOTA > 0` to set a default quota for all users.

The default value `DEFAULT_FILE_QUOTA` is -1, meaning that file quota checking is bypassed.

---

## **Token Handling**
- Tokens are generated at user creation time (`or-<random hex>`).  
- Only a **SHA-256 hash** is stored in the database.  
- During authentication, the incoming Bearer token is hashed and compared with the stored hash.

---

