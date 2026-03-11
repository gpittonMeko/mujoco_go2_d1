# Push su GitHub

La repository è pronta. Per eseguire il push:

```bash
cd /home/lab/Documents/Unitree_Simulator
git push -u origin main
```

**Se SSH fallisce (Host key verification failed):**
1. Aggiungi GitHub a known_hosts: `ssh-keyscan github.com >> ~/.ssh/known_hosts`
2. Verifica di avere una chiave SSH: `ls ~/.ssh/id_*.pub`
3. Se non ce l'hai, generane una: `ssh-keygen -t ed25519 -C "tua@email"` e aggiungila su GitHub → Settings → SSH keys

**Se preferisci HTTPS:**
```bash
git remote set-url origin https://github.com/gpittonMeko/mujoco_go2_d1.git
git push -u origin main
```
(richiederà username e token/password)

**Collaboratori:** su GitHub → Repository → Settings → Collaborators → Add people.
