# Instagram → Telegram Downloader Bot

Bot Telegram qui télécharge **posts, reels, stories et "à la une"** d'un profil
Instagram (ou un post/reel précis) et te les renvoie. Pensé pour **Railway** +
proxy **Webshare.io**.


---

## 1. Pré-requis

- Un **bot Telegram** : écris à [@BotFather](https://t.me/BotFather) → `/newbot`
  → récupère le **token** (de la forme `123456:ABC...`).
- Un **compte Webshare.io** (gratuit) → onglet *Proxy* → *Proxy List* ou
  *Rotating proxy* → tu obtiens un identifiant, un mot de passe et un hôte/port.
- Python 3.11+ en local pour générer les cookies.

---

## 2. Générer les cookies Instagram (en local)

Les stories, la section "à la une" et les comptes privés nécessitent d'être
connecté. La méthode la plus stable sur serveur : injecter des **cookies**.

```bash
pip install instaloader
python make_cookies.py
```

Le script te connecte (gère la 2FA) et affiche une ligne :

```
IG_COOKIES_B64=eyJ...   ← copie-la
```

> Astuce : utilise de préférence un **compte Instagram secondaire** dédié au bot.
> En cas de blocage automatique, c'est lui qui sera affecté, pas ton compte
> principal.

*Alternative sans script* : une extension navigateur type « Get cookies.txt
LOCALLY » sur `instagram.com`, exporte le fichier, puis encode-le :
`base64 -w0 cookies.txt` (Linux/macOS).

---

## 3. Déploiement sur Railway

1. Pousse ce dossier sur un dépôt GitHub (ou utilise *Deploy from local*).
2. Sur [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo**.
3. Onglet **Variables**, ajoute :

| Variable          | Obligatoire | Exemple / note                                            |
|-------------------|:-----------:|-----------------------------------------------------------|
| `BOT_TOKEN`       | ✅          | token de BotFather                                        |
| `IG_COOKIES_B64`  | recommandé  | sortie de `make_cookies.py`                               |
| `PROXY`           | recommandé  | `http://USER-rotate:PASS@p.webshare.io:80`                |
| `ALLOWED_USER_IDS`| optionnel   | ton ID Telegram (via [@userinfobot](https://t.me/userinfobot)) |
| `GROUP_ID`        | optionnel   | `-1001234567890` pour livrer dans un groupe               |
| `MAX_POSTS`       | optionnel   | `30` par défaut                                           |

4. Railway lance `python bot.py` (voir `Procfile`). Un petit serveur HTTP répond
   au *health check* sur `$PORT` automatiquement — pas besoin d'y toucher.

> ⚠️ **N'utilise PAS** les variables `HTTP_PROXY` / `HTTPS_PROXY` : le bot lit
> `PROXY` et applique le proxy **uniquement à Instagram/yt-dlp**, jamais à
> Telegram. Mettre `HTTPS_PROXY` ferait passer Telegram par le proxy et
> provoquerait des timeouts.

---

## 4. Utilisation

Dans Telegram, écris au bot :

- **Un profil** : `@natgeo` ou `https://instagram.com/natgeo`
  → des boutons apparaissent : *Posts · Reels · Stories · À la une · Tout*.
- **Un post/reel précis** : `https://instagram.com/reel/XXXXXXX/`
  → téléchargé directement.

---

## 5. Test rapide en local (optionnel)

```bash
pip install -r requirements.txt
export BOT_TOKEN="..."
export IG_COOKIES_B64="..."
export PROXY="http://USER-rotate:PASS@p.webshare.io:80"
python bot.py
```

---

## 6. Limites & dépannage

- **50 Mo par fichier** : limite de l'API Bot Telegram. Les vidéos plus lourdes
  sont signalées puis ignorées. (Pour dépasser cette limite il faudrait héberger
  un *Local Bot API Server*, hors périmètre ici.)
- **« connexion requise » sur les stories** → `IG_COOKIES_B64` manquant/expiré :
  relance `make_cookies.py`.
- **« limite de débit Instagram »** → Instagram t'a temporairement freiné.
  Attends, baisse `MAX_POSTS`, et garde le proxy actif. Le bot insère déjà des
  pauses aléatoires entre les publications.
- **« compte privé non suivi »** → le compte des cookies doit suivre ce profil.
- **Reels** : Instagram ne sépare pas proprement l'onglet Reels via l'API ;
  le bouton *Reels* récupère les **publications vidéo** (qui incluent les reels).
- Le bot traite **un téléchargement à la fois** (plus sûr pour ton IP/compte).

---

## Fichiers

- `bot.py` — le bot.
- `make_cookies.py` — génère `IG_COOKIES_B64` (à lancer en local).
- `requirements.txt`, `Procfile`, `runtime.txt`, `.env.example`.
