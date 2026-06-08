"""
make_cookies.py — à lancer SUR TON ORDINATEUR (pas sur Railway).

Génère un fichier de cookies Instagram au format Netscape, puis affiche sa
version base64 à coller dans la variable d'environnement IG_COOKIES_B64.

Usage :
    pip install instaloader
    python make_cookies.py

Si tu utilises la 2FA, le script te demandera le code.
"""

import base64
import getpass
import http.cookiejar as cookiejar

import instaloader


def main() -> None:
    user = input("Identifiant Instagram : ").strip()
    L = instaloader.Instaloader(quiet=True, iphone_support=False)

    try:
        L.login(user, getpass.getpass("Mot de passe : "))
    except instaloader.exceptions.TwoFactorAuthRequiredException:
        code = input("Code 2FA : ").strip()
        L.two_factor_login(code)
    except instaloader.exceptions.BadCredentialsException:
        print("❌ Identifiants incorrects.")
        return
    except Exception as exc:
        print(f"❌ Échec de connexion : {exc}")
        print("   (Instagram bloque parfois les nouvelles connexions : réessaie "
              "depuis le même réseau que ton téléphone, ou plus tard.)")
        return

    # Convertit les cookies de la session requests en fichier Netscape
    jar = cookiejar.MozillaCookieJar("cookies.txt")
    for c in L.context._session.cookies:
        jar.set_cookie(c)
    jar.save(ignore_discard=True, ignore_expires=True)

    with open("cookies.txt", "rb") as f:
        b64 = base64.b64encode(f.read()).decode()

    print("\n✅ Fichier 'cookies.txt' créé.\n")
    print("Copie la ligne ci-dessous dans Railway → Variables :\n")
    print("IG_COOKIES_B64=" + b64)
    print("\n(Ne partage JAMAIS cette valeur : elle donne accès à ton compte.)")


if __name__ == "__main__":
    main()
