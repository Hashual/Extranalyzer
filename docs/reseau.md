# Réseau : Pi, proxyServer, WireGuard et WolfNet

État relevé le 2026-11-12 à partir des sorties de commandes des sessions précédentes. Repères :
**[vérifié]** = vu dans la sortie d'une commande sur la machine ; **[supposé]** = déduit, jamais confirmé.
Aucune clé, aucun mot de passe ni aucune IP publique ne doivent figurer ici.

> **Piège n° 1** : il y a **deux réseaux privés distincts** aux adresses voisines.
> - `10.10.0.0/24` : **WireGuard**, serveur sur **proxyServer** (interface `wg0`).
> - `10.10.10.0/24` : **WolfNet**, le réseau maillé de WolfStack, auquel appartient le **Pi** (interface `wolfnet0`).
>
> Le README d'installation parle de `wg0` et `10.8.0.1` sur le Pi : c'est un exemple générique, **faux pour ce Pi**.

## Machines

| Machine | Rôle | Accès |
|---|---|---|
| **Raspberry Pi** (Debian 12, arm64, utilisateur `raspberry`) | Héberge Extranalyzer en Docker | `ssh -i ~/.ssh/extranalyzer_pi raspberry@10.10.10.95` depuis le PC, sans alias SSH |
| **proxyServer** (VPS Oracle Cloud, Ubuntu noble, arm64, utilisateur `ubuntu`) | Serveur WireGuard, nginx, nœud WolfStack | Alias SSH `proxyServer` |
| **PC Windows** (Claude Desktop) | Poste de l'utilisateur. Les sessions Claude y lancent `ssh`/`curl` directement | Tunnel WireGuard `Acces_wolfStack`, adresse `10.10.0.2` |
| **Téléphones** `telephone1` (`10.10.0.3`) et `telephone2` (`10.10.0.4`) | Clients WireGuard | Voir « Questions ouvertes » |

Le « **proxy** » désigne la machine **proxyServer**, pas un proxy HTTP. Aucun `HTTP_PROXY`, squid, tinyproxy
ni SOCKS n'est en jeu.

## Le Pi

Interfaces **[vérifié]** :
```
eth0      192.168.1.95/24   LAN de la maison
wolfnet0  10.10.10.95/24    WolfNet (device TUN, mtu 1400 ; ce n'est pas du WireGuard)
lxcbr0    10.0.3.1/24       LXC
docker0   172.17.0.1/16     + bridges br-* en 172.18–20.0.1/16
```

- **WolfNet** est fourni par `wolfnet.service` (« WolfNet - Secure Private Mesh Networking »). Il fait partie de la
  plateforme **WolfStack** : `wolfstack.service`, `wolfusb.service`, `wolfstack-mounts-wait.service`,
  `wolfstack-mounts.target`. **[vérifié]**
- `wg-quick@wg0` est **inactif** sur le Pi : il n'y a **pas de WireGuard sur le Pi**. **[vérifié]**
- **Extranalyzer** écoute uniquement sur `10.10.10.95:8000` (`BIND_ADDR=10.10.10.95` dans `~/extranalyzer/.env`) ;
  `http://192.168.1.95:8000` ne répond pas, et c'est voulu. **[vérifié]**
- **Ordre de démarrage** : le drop-in Docker `extranalyzer-wolfnet.conf` contient
  `After=wolfnet.service` / `Wants=wolfnet.service`, à côté de `wolfstack-mounts.conf` installé par WolfStack.
  La recette du README (`After=wg-quick@wg0`) **ne s'applique pas** à ce Pi. **[vérifié]**
- Vaultwarden occupe `127.0.0.1:8000`, sans conflit avec `10.10.10.95:8000`. **[vérifié]**
- Un conteneur **`proxy-frpc-1`** (client frp) tourne sur le Pi. Il publie probablement des services du Pi via
  frps sur proxyServer, peut-être le port `[proxyServer]:2222` vu dans known_hosts. **[supposé]**
- Sortie internet directe : `docker pull` depuis ghcr.io et l'appel à `api.enablebanking.com` fonctionnent depuis le
  Pi. La route exacte n'a pas été vérifiée.
- La limite mémoire de 256 Mo du conteneur est sans effet : les cgroups mémoire sont désactivés sur le Pi.
  **[vérifié]**
- L'entrée `192.168.1.96` de known_hosts est périmée et ne correspond pas au Pi.

## proxyServer et WireGuard

- Serveur **WireGuard** `wg0` : `10.10.0.1/24`, port UDP `51820`, `ip_forward=1`, MASQUERADE vers `eth0`.
  **[vérifié]**

  | Pair | Adresse | État |
  |---|---|---|
  | PC Windows | `10.10.0.2/32` | Handshake récent **[vérifié]** |
  | telephone1 | `10.10.0.3/32` | Atteint Extranalyzer (confirmé par l'utilisateur le 2026-11-13) **[vérifié]** |
  | telephone2 | `10.10.0.4/32` | Config prête, import non confirmé |

- Les configs clientes des téléphones sont dans `/etc/wireguard/clients/telephone{1,2}.conf`. Elles utilisent
  `AllowedIPs = 10.10.0.0/24, 10.10.10.95/32` (split tunnel : le sous-réseau WireGuard plus le seul Pi) et
  `PersistentKeepalive = 25`. La sauvegarde `wg0.conf.bak-20261112-182051` a été faite avant l'ajout des
  téléphones. QR code en PNG : `ssh proxyServer "sudo cat /etc/wireguard/clients/telephone1.conf | qrencode -o - -t PNG -s 8" > telephone1.png`
  (`qrencode` est installé ; en mode texte, le QR est souvent tronqué par un terminal trop petit).
- proxyServer est aussi un nœud **WolfNet** : `wolfnet0` = `10.10.10.47/24`, route `10.10.10.0/24 dev wolfnet0`,
  et une règle NAT `POSTROUTING ! -s 10.10.10.0/24 -o wolfnet0 -j MASQUERADE`. Le Pi voit donc le trafic des
  clients WireGuard comme venant de `10.10.10.47` et n'a besoin d'aucune route vers `10.10.0.0/24`. **[vérifié]**
- **ufw** est actif. Il autorise 22, 80, 443 (Nginx Full), 8553/tcp, 9600/udp et 51820/udp. La politique FORWARD
  est DROP, sauf `-i wg0`, `-i/-o wolfnet0` et RELATED,ESTABLISHED. **[vérifié]**
- **Bug connu, non corrigé** : le `PostDown` de `wg0.conf` fait `iptables -t nat -A POSTROUTING …` au lieu de
  `-D`.
- La config SSH du PC contient un autre VPS, `portfolcraft`, dont le rôle est inconnu.

## Chemins

```
PC ou téléphone ──WireGuard (10.10.0.x → 10.10.0.1)──► proxyServer ──NAT, WolfNet (10.10.10.47)──► Pi 10.10.10.95:8000
```
Le PC et telephone1 atteignent `http://10.10.10.95:8000`, et proxyServer lui-même aussi (réponse 200). **[vérifié]**

Une configuration WireGuard n'envoie dans le tunnel que les plages de ses `AllowedIPs`. Avec seulement
`AllowedIPs = 10.10.0.0/24`, `10.10.10.95` partait hors du tunnel : c'était la cause du blocage de telephone1,
corrigée en ajoutant `10.10.10.95/32`. **[vérifié]**

## Pour une session Claude

- Commencer par relire ce fichier, puis vérifier sur la machine avant d'affirmer quoi que ce soit
  (`ip -4 -o addr`, `systemctl status wolfnet`, `sudo wg show` sur proxyServer).
- Depuis PowerShell, les guillemets des commandes `ssh` passées en argument se cassent. Il vaut mieux déposer un
  script sur la machine, ou utiliser le shell Bash.
- Un push sur `main` reconstruit l'image `ghcr.io/hashual/extranalyzer` (GitHub Actions) : c'est un
  redéploiement du Pi.
- Enable Banking : `EB_REDIRECT_URL=https://localhost/bank/callback`, car Enable Banking refuse les retours en
  http. Le retour se fait donc par copier-coller de l'adresse dans l'onglet Banque.

## Questions ouvertes

- Il faut encore importer la config de telephone2, puis supprimer les fichiers `clients/*.conf` du serveur.
- Quel est le rôle de frpc/frps, du port 2222, de 8553/tcp, de 9600/udp et du VPS `portfolcraft` ?
- Quels sont le hostname du Pi et son pare-feu (jamais inspecté) ?
- Il reste à corriger le `PostDown` de `wg0.conf`.
