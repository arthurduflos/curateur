# Curateur podcasts / YouTube

Chaque lundi : récupère les nouvelles vidéos de tes émissions de confiance et des invités
que tu suis, les fait noter par Claude selon ton profil, ajoute les meilleures à la playlist
YouTube du mois en cours et publie un rapport (onglet Actions > dernier run > Summary).

## Mise en place (≈ 20 min, une seule fois)

1. **Google Cloud** (console.cloud.google.com) : créer un projet, activer
   « YouTube Data API v3 », configurer l'écran de consentement OAuth (type Externe,
   ajoute ton adresse en utilisateur de test), puis créer des identifiants
   OAuth de type **Application de bureau** et télécharger le JSON en `client_secret.json`.
   Pense à passer l'app en « Production » dans l'écran de consentement,
   sinon le refresh token expire au bout de 7 jours.
2. **Token** : `pip install -r requirements.txt && python auth_setup.py`,
   connecte-toi avec le compte YouTube qui possède la playlist.
3. **Playlists** : rien à faire, le script crée automatiquement une playlist par mois
   (« Curation — Octobre 2026 », « Curation — Novembre 2026 »…) dès la première
   vidéo retenue du mois. Nom et visibilité réglables dans `config.yaml`.
4. **GitHub** : repo privé, pousse ces fichiers (sans `client_secret.json`),
   puis Settings > Secrets > Actions : `ANTHROPIC_API_KEY`, `YT_CLIENT_ID`,
   `YT_CLIENT_SECRET`, `YT_REFRESH_TOKEN`.
5. Onglet Actions > « Curation hebdo » > Run workflow pour un premier test.

## Réglages

Tout se passe dans `config.yaml` : chaînes, invités, seuil, et surtout `profile`,
à enrichir quand un épisode te déçoit ou t'enthousiasme. C'est ce qui rend la
sélection meilleure avec le temps.
