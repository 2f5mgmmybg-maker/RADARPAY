RADAR PAYMENT — déploiement et exploitation

ARCHITECTURE
- Le dépôt 2f5mgmmybg-maker/RADARPAY contient index.html, photos.json, photos/,
  bot.py et requirements.txt.
- GitHub Pages publie la mini-app (index.html, photos.json et photos/).
- Render exécute bot.py comme Web Service : le bot Telegram et l'API HTTP tournent
  dans le même processus. Ne pas le configurer comme Background Worker.
- Branche utilisée : main.

RENDER
- Build command : pip install -r requirements.txt
- Start command : python bot.py
- Le service doit écouter sur le port PORT fourni par Render.
- Health Check Path conseillé : /health. Cette route vérifie les URL HTTPS de l'API et de la mini-app,
  la présence du code ADMIN et PostgreSQL lorsqu'il est configuré. Elle renvoie HTTP 503 si une
  configuration essentielle manque, si l'état privé n'est pas lisible ou si PostgreSQL est indisponible.
  L'état NOWPayments est affiché séparément dans le champ payments. En mode GitHub seul, le stockage
  reste signalé comme dégradé; les accès GitHub/Telegram restent à contrôler dans les journaux Render.

VARIABLES D'ENVIRONNEMENT
- BOT_TOKEN : token Telegram du bot (secret).
- OWNER_ID : identifiant numérique Telegram du propriétaire.
- ADMIN_ACCESS_CODE : code secret administrateur de 6 chiffres, obligatoire pour ouvrir l'espace ADMIN.
  La session ADMIN expire après 12 h et doit être rouverte après un redémarrage du service.
- GITHUB_TOKEN : token autorisé à lire/écrire le dépôt RADARPAY (secret).
- GITHUB_REPO : 2f5mgmmybg-maker/RADARPAY.
- GITHUB_BRANCH : main.
- SITE_URL : URL HTTPS publique de la mini-app GitHub Pages.
- API_BASE_URL : facultatif; URL HTTPS publique du Web Service Render, sans slash final.
  Si vide, le code utilise RENDER_EXTERNAL_URL fourni par Render.
- NOWPAYMENTS_API_KEY : clé API privée du compte NOWPayments (secret).
- NOWPAYMENTS_IPN_SECRET : secret IPN configuré dans NOWPayments (secret).
- URL IPN envoyée automatiquement à chaque facture : https://radarpay-90js.onrender.com/nowpayments/ipn
  (ou API_BASE_URL + /nowpayments/ipn si l'URL Render change; aucune saisie globale supplémentaire n'est nécessaire).
- Le catalogue TARGET NUMÉRIQUE facture 25 EUR; le CALLER choisit la cryptomonnaie
  sur la page de paiement hébergée NOWPayments. Seul un IPN signé avec le statut
  payment_status=finished passe automatiquement la commande à paid.
- DATA_ENCRYPTION_KEY : recommandé; secret aléatoire stable utilisé pour chiffrer
  radar_private.enc, les captures privées et les vocaux.
  À générer une seule fois avec :
  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  Conserver dans un gestionnaire de secrets. Ne jamais le publier ni le changer sans
  procédure de migration et sauvegarde.
  Au premier démarrage avec cette variable, l'ancien radar_private.enc chiffré avec
  la clé dérivée de BOT_TOKEN est migré automatiquement.

SÉCURITÉ ET EXPLOITATION
- Ne jamais partager BOT_TOKEN, GITHUB_TOKEN ou DATA_ENCRYPTION_KEY.
- Ne pas ajouter de données sensibles dans photos.json ou dans le dossier photos/ :
  ces contenus sont publiés par GitHub Pages.
- radar_private.enc est chiffré, mais le dépôt RADARPAY est public. La sécurité dépend
  donc de la conservation des clés et des sauvegardes.
- Garder une seule instance Render active. Les verrous, limites de débit et caches sont
  en mémoire; plusieurs instances ne partagent pas ces états. GitHub Contents API n'est
  pas une base transactionnelle et ne convient pas à une montée en charge multi-instance.
- Les suppressions de médias GitHub échouées sont consignées dans les journaux Render.
  Vérifier ces journaux en cas de suppression incomplète.
- Les preuves de paiement sont des déclarations non vérifiées. Sans intégration API/webhook
  du prestataire de paiement, l'application ne peut pas confirmer qu'un paiement est réglé.
- Si une preuve de transaction dépasse son délai de 2 minutes, l'état proof_expired
  bloque la vente; aucun mécanisme de reprise automatique n'est prévu.

DÉPLOIEMENT
- GitHub Pages publie le frontend depuis main.
- Render doit être connecté au dépôt RADARPAY, branche main, avec les commandes ci-dessus.
- Après chaque commit, vérifier l'exécution GitHub Actions « Backend syntax check »
  et l'événement de déploiement Render.
- Vérifier aussi le service en ouvrant l'URL Render; une compilation réussie ne prouve
  pas que les variables Render, Telegram et GitHub fonctionnent ensemble.
- Ne jamais remplacer photos.json en production par une ancienne copie locale :
  cela pourrait écraser le catalogue publié.
