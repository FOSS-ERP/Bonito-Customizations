# ERPNext Backup Restoration – Issue Resolution Runbook

**Site:** bonito.fosscrm.com  
**Server IP:** 168.144.22.195  
**ERPNext/Frappe Branch:** version-16  
**Date of Restoration:** April 2026  
**Backup File:** `20260409_191604-replica-erpnext_bonito_in-database.sql.gz`

---

## Overview

This document records every issue encountered and resolved during the fresh server provisioning and backup restoration of the ERPNext site `bonito.fosscrm.com`.

---

## Phase 1 – Server Preparation

### Steps Performed

```bash
sudo timedatectl set-timezone UTC
sudo apt-get update -y && sudo apt-get upgrade -y
sudo apt-get install git curl python3-dev python3-pip python3-setuptools -y
sudo apt-get install python3-venv software-properties-common xvfb libfontconfig -y
sudo apt-get install libmysqlclient-dev pkg-config redis-server -y
sudo pip3 install frappe-bench --break-system-packages
```

---

## Phase 2 – Frappe Bench Initialization

```bash
bench init --frappe-branch version-16 frappe-bench
cd frappe-bench
chmod -R o+rx /home/frappe
```

---

## Phase 3 – Backup File Transfer

### Issue 1 – SCP Failed: Local File Not Found

**Error:**
```
scp: stat local "/root/Downloads/20260409_191604-replica-erpnext_bonito_in-database.sql.gz": No such file or directory
```

**Cause:**  
The backup file had already been downloaded to the local machine but the path used in the `scp` command pointed to the wrong directory (`~/Downloads/`).

**Resolution:**  
Verified the correct local path and re-ran the `scp` command with the right source path:
```bash
scp -i ~/.ssh/id_ed25519 <correct-local-path>/20260409_191604-replica-erpnext_bonito_in-database.sql.gz root@168.144.22.195:/root/
```

Then moved and fixed ownership on the server:
```bash
mv /root/20260409_191604-replica-erpnext_bonito_in-database.sql.gz /home/frappe/frappe-bench/
chown frappe:frappe /home/frappe/frappe-bench/20260409_191604-replica-erpnext_bonito_in-database.sql.gz
```

---

## Phase 4 – Site Creation and Restore

### Issue 2 – `bench restore` Failed Without a Site

**Error:**  
Running `bench restore` without specifying a site resulted in an error because no default site existed yet.

**Resolution:**  
Created the site first, then used the `--site` flag:
```bash
bench --site bonito.fosscrm.com restore /home/frappe/frappe-bench/20260409_191604-replica-erpnext_bonito_in-database.sql.gz
```

---

### Issue 3 – Corrupted or Incompatible SQL Dump (Direct MySQL Import Needed)

**Cause:**  
The `bench restore` command failed mid-way due to incompatibilities in the SQL dump (table collation issues or partial dump). The backup was from a replica and contained statements that caused errors.

**Resolution:**  
The SQL dump was pre-processed to strip problematic lines, saved as `/tmp/fixed_backup.sql`, and imported directly via MariaDB client:
```bash
mysql -u _a988871720412cdc -pyhnKr4euuDKPdYaO _a988871720412cdc < /tmp/fixed_backup.sql
```

Then bench restore was used only for metadata/config reconciliation:
```bash
bench --site bonito.fosscrm.com restore /tmp/fixed_backup.sql
```

---

## Phase 5 – Migration Issues

### Issue 4 – Out of Memory During `bench migrate`

**Symptom:**  
Migration process was killed by the OOM (Out of Memory) killer. The server had no swap space, and the MariaDB dump of large tables (e.g., `tabData Import`) caused memory exhaustion.

**Error seen in backup.log:**
```
mariadb-dump: Error 2013: Lost connection to server during query when dumping table `tabData Import` at row: 312
```

**Root Cause:**  
No swap partition/file was configured on the fresh server.

**Resolution:**  
Added a 2 GB swap file:
```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

Swap is now persistent across reboots (`/etc/fstab` updated). Verified:
```
NAME      TYPE  SIZE  USED  PRIO
/swapfile file    2G  1.3M    -2
```

---

### Issue 5 – Migration Failed on `DocType State` Table

**Error:**  
Migration aborted due to a schema sync error on the `DocType State` table.

**Resolution:**  
Ran a targeted DB update for the affected DocType, then re-ran migrate:
```bash
bench --site bonito.fosscrm.com execute "frappe.db.updatedb" --args "['DocType State']"
bench --site bonito.fosscrm.com migrate
```

---

## Phase 6 – SSL / HTTPS Setup

### Issue 6 – `bench setup lets-encrypt` Failed Initially

**Cause:**  
DNS multitenancy was not enabled before attempting Let's Encrypt setup.

**Resolution:**  
Enabled DNS multitenancy first, then ran Let's Encrypt:
```bash
bench config dns_multitenant on
sudo -H bench setup lets-encrypt bonito.fosscrm.com
```

When automated validation failed (DNS propagation timing), the certificate was obtained manually:
```bash
sudo certbot certonly --manual --preferred-challenges dns -d bonito.fosscrm.com
```

---

## Phase 7 – Final Steps

```bash
bench set-admin-password Book123!
bench clear-cache && bench clear-website-cache
sudo supervisorctl restart frappe-bench-web:frappe-bench-frappe-web
bench restart
```

---

## Final System State

| Item | Value |
|---|---|
| Site | bonito.fosscrm.com |
| Frappe Branch | version-16 |
| Swap | 2 GB (`/swapfile`) |
| SSL | Let's Encrypt (DNS challenge) |
| DNS Multitenant | Enabled |
| Default Site | bonito.fosscrm.com |
| Admin Password | Reset post-restore |

---

## Summary of Issues and Fixes

| # | Issue | Root Cause | Fix |
|---|---|---|---|
| 1 | SCP failed – file not found | Wrong local path in scp command | Corrected path, retransferred file |
| 2 | `bench restore` failed – no site | Site not created before restore | Created site first, used `--site` flag |
| 3 | SQL dump import errors | Replica dump had incompatible statements | Pre-processed dump, imported via mysql directly |
| 4 | OOM during migrate / mariadb-dump lost connection | No swap space on fresh server | Added 2 GB persistent swap file |
| 5 | Migration failed on `DocType State` | Schema sync error | Ran `frappe.db.updatedb` on the table, re-migrated |
| 6 | Let's Encrypt setup failed | DNS multitenant not enabled; DNS propagation delay | Enabled multitenant, used manual DNS challenge |
