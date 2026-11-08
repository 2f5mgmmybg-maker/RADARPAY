RADAR PAYMENT — consignes de déploiement

1) Le service Render qui exécute bot.py doit être un Web Service (pas un Background Worker),
   car il héberge aussi l'API HTTP et écoute sur le port fourni par Render.

2) Variables d'environnement :
   BOT_TOKEN       = token actuel du bot Telegram
   OWNER_ID        = identifiant numérique Telegram du propriétaire
   GITHUB_TOKEN    = token GitHub autorisé à lire/écrire le dépôt RADARPAY
   GITHUB_REPO     = 2f5mgmmybg-maker/RADARPAY
   GITHUB_BRANCH   = main (ou la branche réelle qui contient index.html/photos.json)
   SITE_URL        = URL HTTPS publique de la mini-app
   ACCESS_CODE     = 134679
   API_BASE_URL    = facultatif; URL HTTPS publique de ce Web Service Render, sans slash final.
                     Si vide, le code utilise RENDER_EXTERNAL_URL fourni par Render.
   DATA_ENCRYPTION_KEY = recommandé; secret aléatoire stable utilisé pour chiffrer les données privées.
                         À générer une fois (ex. : python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"),
                         à conserver dans un gestionnaire de secrets et à ne jamais changer sans sauvegarde.
                         Au premier démarrage avec cette variable, l’ancien fichier radar_private.enc
                         est migré automatiquement depuis la clé dérivée de BOT_TOKEN.

3) Ne pas mettre GITHUB_REPO=radar-bot : ce dépôt contient le code du bot, pas les données de la mini-app.

4) Le fichier photos.json livré dans cette archive est la copie originale reçue. Ne le téléverse pas
   par-dessus le fichier en production : il pourrait remplacer la liste des photos en ligne.
   Le bot actualise seulement sa propriété "api" et conserve les propriétés "photos" existantes.

5) Déployer index.html dans RADARPAY. Déployer bot.py dans radar-bot.
   requirements.txt ne change pas.
