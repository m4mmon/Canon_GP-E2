# Canon GP-E2 — description du protocole USB

Oct 19, 2026 · @raph

Le GP-E2 parle PTP sur USB, avec des opérations Canon spécifiques : 0x91A0–0x91AA face à un PC, 0x91D5–0x91DE face à un appareil photo. Ce document décrit les deux usages ; le côté appareil photo a été vérifié avec un EOS 700D (firmware 1.1.5), les autres boîtiers restent à tester.

## Identification USB

Le GP-E2 a une prise mini-USB AB : en mini-B il est **périphérique** (face à un PC), en mini-A il est **hôte** (face à l'appareil photo, câble mini-A ↔ mini-B). L'interrupteur doit être sur ON, pas LOG, pour la connexion au PC.

| Élément | Valeur |
| --- | --- |
| VID:PID | 04A9:3251, bcdDevice 0x0001, bcdUSB 2.00 |
| Chaînes | 1 = « Canon Inc. », 2 = « Canon GP-E2 », pas de n° de série USB |
| Vitesse | Full Speed (12 Mb/s), bMaxPacketSize0 = 64 |
| Configuration | 1 configuration, 39 o, bmAttributes 0xC0 (auto-alimenté), MaxPower 100 mA ; GET\_STATUS → 0x0001 |
| Interface 0 | classe 06 / 01 / 01 (Still Image, PTP), 3 endpoints |
| Endpoints | 0x81 bulk IN 64 o ; 0x02 bulk OUT 64 o ; 0x83 interrupt IN 8 o, intervalle 10 (jamais utilisé) |
| Requêtes de classe | Get Device Status (0x67) → OK ; Cancel (0x64) ; Device Reset (0x66) |

Conteneurs PTP standard, petit-boutistes : `u32 longueur, u16 type (1 commande, 2 données, 3 réponse, 4 événement), u16 code, u32 transaction`, puis les paramètres u32 ou les données. Quand un conteneur IN fait un multiple exact de 64 o, le GP-E2 envoie ensuite un paquet vide (ZLP).

Codes de réponse utilisés : 0x2001 OK, 0x2005 opération non supportée, 0x2019 occupé (Device\_Busy).

## Connexion au PC

Le PC ouvre une session puis passe le GP-E2 en mode distant ; aucune opération de stockage PTP standard n'est nécessaire. Séquence d'initialisation :

1. OpenSession (0x1002), paramètre 1.
2. GetDeviceInfo (0x1001).
3. GetDeviceInfoEx Canon (0x9108).
4. SetRemoteMode (0x9114), paramètre 1.
5. 0x91A7 (mode courant) : paramètre de réponse 0 = normal, 1 = updater. Un outil de lecture doit s'arrêter si la valeur n'est pas 0.

**GetDeviceInfo** (187 o) : extension vendeur 0x0000000B (Canon), version 200, PTP 1.00 ; fabricant « Canon Inc. », modèle « Canon GP-E2 », version « 4-2.0.2 », n° de série (10 chiffres) ; aucun événement ni propriété déclarés ; format 0x3801. 33 opérations en mode normal : 0x1001–0x100A, 0x101B, 0x1014–0x1016, 0x9108, 0x9113, 0x9114, 0x9013, 0x911D, 0x911E, 0x91A0–0x91AA, 0x91FE, 0x91FF.

**GetDeviceInfoEx** (52 o) : `u32 longueur (0x34), u32 0, u32 7`, puis 7 codes de propriété en u32 (0xD16C, 0xD16D, 0xD14D, 0xD14E, 0xD14F, 0xD16E, 0xD16F), puis `u32 2, u32 0, u32 0, u32 1`.

Stockage annoncé : 0x80000001, label « V », RAM intégrée, système de fichiers propriétaire 0x8001, capacité 0. Les journaux ne passent pas par les objets PTP mais par les opérations 0x91Ax.

| Opcode | Fonction | Paramètres | Données / réponse |
| --- | --- | --- | --- |
| 0x91A0 | passer en mode updater (redémarrage) | – | – |
| 0x91A1 | envoyer un bloc de firmware | offset, longueur | données hôte → GP-E2 |
| 0x91A2 | effacer **tous** les journaux | – | réponse immédiate |
| 0x91A3 | lister les journaux | – | liste (voir plus bas) |
| 0x91A4 | lire un journal | handle, offset, taille | bloc ; paramètre de réponse = octets lus |
| 0x91A5, 0x91A6 | inconnus | – | – |
| 0x91A7 | mode courant | – | paramètre 0 normal, 1 updater |
| 0x91A8 | début de mise à jour | taille du .FIR | – |
| 0x91A9 | bloc demandé par le GP-E2 | – | paramètres offset, longueur ; (0, 0) = fin |
| 0x91AA | état d'une opération longue | – | 0x2019 tant qu'elle dure, puis 0x2001 |

## Journaux

Un journal par jour (nom `AAAAMMJJ.log`), lu par blocs de la taille donnée par la propriété 0xD16E (8192 o). Chaque point fait 32 octets ; le NMEA est reconstruit côté PC.

**0x91A3, liste** : `u32 nombre`, puis 44 o par journal :

| Offset | Taille | Contenu |
| --- | --- | --- |
| 0 | 4 | handle (u32) |
| 4 | 16 | nom ASCII complété par des 0 (« 20170809.log ») |
| 20 | 4 | taille en octets (u32, multiple de 32) |
| 24 | 4 | date de début : JJ MM AA 00 |
| 28 | 4 | heure de début : hh mm ss 00 |
| 32 | 4 | 0 |
| 36 | 4 | heure de fin : hh mm ss 00 |
| 40 | 4 | 0 |

**0x91A4, lecture** : paramètres handle, offset, taille (0x2000) ; le GP-E2 renvoie le bloc et, en paramètre de réponse, le nombre d'octets lus. On boucle jusqu'à la taille annoncée.

**Point de 32 octets** (petit-boutiste) :

| Octet | Contenu |
| --- | --- |
| 0 | 0x5A, marqueur de début |
| 1 | drapeaux : bit 0 altitude négative ; bit 1 inconnu (toujours 1) ; bit 2 Est (0 = Ouest) ; bit 3 Nord (0 = Sud) ; bits 4–7 à 0 |
| 2–4 | date JJ MM AA |
| 5–7 | heure UTC hh mm ss |
| 8–9 | millisecondes (u16) |
| 10 | latitude, degrés |
| 11 | latitude, minutes |
| 12–13 | latitude, fraction de minute × 10000 (u16) |
| 14–15 | longitude, degrés (u16) |
| 16 | longitude, minutes |
| 17–18 | longitude, fraction de minute × 10000 (u16) |
| 19–20 | altitude en mètres, valeur absolue (u16) |
| 21 | nombre de satellites |
| 22–23 | HDOP : partie entière, décimale |
| 24 | inconnu (21–36 ; probablement le meilleur rapport signal/bruit en dB-Hz) |
| 25–30 | 0 |
| 31 | 0xA5, marqueur de fin |

Valeurs habituelles de l'octet 1 : 0x0E N/E, 0x0A N/W, 0x06 S/E, 0x02 S/W (+1 si altitude négative).

**Fichier .log de référence** (celui de GPS Log File Utility) : première ligne `@CanonGPS/ver1.0/wgs-84/Canon GP-E2/<n° de série>`, puis une paire `$GPGGA` / `$GPRMC` par point, fins de ligne CRLF, y compris la dernière. Les degrés sont écrits sur 2 chiffres (latitude) et 3 chiffres (longitude).

## Propriétés et intervalle

Les réglages passent par les opérations PTP standard GetDevicePropDesc (0x1014), GetDevicePropValue (0x1015) et SetDevicePropValue (0x1016). L'intervalle d'enregistrement n'a pas d'opération spéciale.

| Propriété | Type | Accès | Valeur | Rôle |
| --- | --- | --- | --- | --- |
| 0xD14D | UINT32 | lecture/écriture | défaut 15 ; énum. 1, 5, 10, 15, 30, 60, 120, 300 | intervalle en secondes (accès côté appareil photo) |
| 0xD14E | UINT32 | lecture | 600 | inconnu |
| 0xD14F | UINT32 | lecture/écriture | même énumération | intervalle en secondes (accès côté PC) |
| 0xD16C | STRING | lecture | « 1910500208 » | n° de série |
| 0xD16D | UINT32 | lecture | 0x80000323 | identifiant de modèle (= 4 premiers octets du .FIR) |
| 0xD16E | UINT32 | lecture | 8192 | taille de bloc pour 0x91A4 |
| 0xD16F | UINT32 | lecture | 8192 | taille de bloc (écriture ou firmware ?) |

D14D et D14F sont une seule valeur vue sous deux codes : le GP-E2 les garde synchronisées, et un réglage fait depuis l'appareil photo se retrouve dans les deux. Descripteur de D14F : type 0x0006, accès 1, défaut 15, valeur courante, forme 0x02 (énumération) de 8 valeurs. Une valeur hors énumération doit être refusée (0x201C).

## Effacement

L'effacement porte toujours sur **tous** les journaux et dure environ 14,5 s, quel que soit le contenu.

1. 0x91A2 (sans paramètre) → 0x2001 immédiatement : l'effacement démarre.
2. 0x91AA (sans paramètre) toutes les 0,5 s environ → 0x2019 tant que l'effacement dure, puis 0x2001.
3. 0x91A3 renvoie ensuite 0 journal (`00 00 00 00`).

Il n'existe pas d'effacement sélectif. Un outil de lecture ne doit jamais envoyer 0x91A2 par erreur.

## Mise à jour du firmware

Le fichier .FIR est envoyé tel quel, par blocs que le GP-E2 demande lui-même ; toute l'opération dure environ 22 s plus le redémarrage. La même version peut être réinstallée.

1. **Mode normal** : initialisation habituelle, puis GetDevicePropValue 0xD16D. Elle doit valoir l'identifiant de modèle du .FIR (0x80000323, ses 4 premiers octets).
2. 0x91A7 → 0, puis **0x91A0** (sans paramètre) : le GP-E2 redémarre en mode updater. Il faut rouvrir une session ; les numéros de transaction repartent à 0.
3. **Mode updater** : GetDeviceInfo ne liste plus que 27 opérations (0x91A0, 0x91A1, 0x91A7, 0x91A8, 0x91A9, sans 0x91A2–0x91A4) ; 0x91A7 renvoie 1.
4. **0x91A8**(taille du .FIR, ex. 0x801EC = 524 780 o) : début du transfert.
5. **Boucle pilotée par le GP-E2** : 0x91A9 → paramètres de réponse (offset, longueur) du bloc voulu ; l'hôte répond par **0x91A1**(offset, longueur) avec ce morceau du fichier en phase de données. On recommence jusqu'à ce que 0x91A9 renvoie (0, 0).
6. Le GP-E2 redémarre en mode normal (environ 7 s) ; 0x91A3 renvoie alors 0 journal.

Ordre des blocs demandés pour 2.0.2 (131 blocs) : en-tête 0x0–0x400, puis un premier passage de 0x130 à la fin (\~0,08 s par bloc), puis un second de 0x1E4 à la fin (\~0,13 s par bloc). Le premier passage ressemble à une vérification, le second à l'écriture en flash.

**En-tête du .FIR** (gpe20202.FIR, 524 780 o) :

| Offset | Contenu |
| --- | --- |
| 0x00 | u32 0x80000323 (identifiant de modèle) |
| 0x10 | version en ASCII (« 2.0.2 ») |
| 0x20 | 4 o, somme de contrôle probable |
| 0x30 | 0x130, début des données |
| 0x38 | 0x801EC, taille du fichier |
| 0x60 | 0x130 / 0x800BC : début et longueur de section |
| 0x68, 0x88 | 20 o chacun (empreintes SHA-1 probables) |

Les données à partir de 0x130 sont chiffrées (entropie 7,99 bits/octet, aucun bloc répété) : on ne peut ni lire ni modifier le firmware, seulement le réinstaller.

## Connexion à l'appareil photo

Les rôles s'inversent : le GP-E2 est **hôte USB et initiateur PTP**, l'appareil photo est le répondeur. Le GP-E2 pousse ses données avec des opérations Canon et relit périodiquement les événements du boîtier ; l'appareil ne demande jamais rien directement.

**Initialisation** (acceptée par l'EOS 700D, qui ouvre alors ses menus GPS) :

1. OpenSession (0x1002), paramètre 1.
2. GetDeviceInfo (0x1001).
3. SetRemoteMode (0x9114), paramètre 9 : la combinaison de bits 0x9 identifie un récepteur GPS de type GP-E2.
4. SetEventMode (0x9115), paramètre 1.
5. **0x91D5**, paramètre 1 : GPS connecté.

Pour se déconnecter : 0x91D5 paramètre 0 (le boîtier affiche « GPS non connecté »), puis CloseSession (0x1003).

**Opérations GPS** (GP-E2 → appareil, réponse 0x2001) :

| Opcode | Paramètres | Données envoyées | Rôle |
| --- | --- | --- | --- |
| 0x91D5 | 1 / 0 | – | GPS connecté / déconnecté |
| 0x91D7 | – | – | interroge l'horloge du boîtier ; réponse (1, valeur), 0 sur le 700D (pas d'horloge haute précision) |
| 0x91D8 | action (3 = fin de mise à l'heure) | – | signal de seconde / fin de synchro d'heure |
| 0x91D9 | – | `[u32 longueur][128 o]` | position, altitude, heure et qualité |
| 0x91DA | – | `[u32 16][u32 statut][u32 niveau][u32 état]` | réception satellites |
| 0x91DB | – | `[u32 48][struct tm]` | heure UTC pour la mise à l'heure |
| 0x91DC | – | données | accepté sans effet visible |
| 0x91DD | – | `[u32 16][réf.][octet][u16 0][u32 num][u32 déno]` | direction (boussole) |
| 0x91DE | niveau | – | état de la batterie |
| 0x9116 | – | (reçu) événements | relevé des événements du boîtier |

**Boucle de fonctionnement** : GetEvent (0x9116) environ toutes les secondes ; à chaque intervalle, 0x91D9 puis 0x91DA et 0x91DE (0x91DD si la boussole est utilisée). Par défaut, envoi toutes les 15 s, puis à l'intervalle choisi dans le menu GPS du boîtier.

GetEvent renvoie une suite d'enregistrements `[u32 taille][u32 type][charge utile]`, terminée par un enregistrement de taille 8 et de type 0.

| Événement | Charge utile | Signification |
| --- | --- | --- |
| 0xC189 | `u32 0xD14D, u32 secondes` | intervalle choisi dans le menu GPS ; au départ la valeur peut être hors plage (ex. 0x3C00) et doit être ignorée |
| 0xC1ED | `u32 0` | demande d'heure : l'utilisateur a validé « Appliquer » |
| 0xC1EC | `u32 5` | heure reçue et écrite dans l'horloge du boîtier |
| 0xC1EB | `u32 0` | mise à l'heure annulée |
| 0xC1EE | `u32 100` ou `u32 0` | ouverture / fermeture d'écrans GPS (rôle exact incertain) |
| 0xC1DD | `u32 1` | à la connexion (rôle inconnu) |
| 0xC1A4, 0xC18E | divers | à la connexion (rôle inconnu) |

Le premier GetEvent renvoie aussi l'état complet des propriétés du boîtier (plusieurs Ko), qu'on peut ignorer.

## Infos GPS (0x91D9)

La position est une structure de 128 octets calquée sur les balises GPS de l'EXIF : références en texte, valeurs en rationnels `{u32 numérateur, u32 dénominateur}`, petit-boutiste. Les données de 0x91D9 sont `[u32 longueur = 132][structure]`. Les champs se retrouvent tels quels dans l'EXIF des photos.

| Offset | Taille | Contenu | EXIF |
| --- | --- | --- | --- |
| 0x00 | 4 | « N » ou « S » + zéros | GPSLatitudeRef |
| 0x04 | 24 | latitude : 3 rationnels | GPSLatitude |
| 0x1C | 4 | « E » ou « W » + zéros | GPSLongitudeRef |
| 0x20 | 24 | longitude : 3 rationnels | GPSLongitude |
| 0x38 | 1 (+3) | 0 = au-dessus, 1 = au-dessous du niveau de la mer | GPSAltitudeRef |
| 0x3C | 8 | altitude en mètres (rationnel) | GPSAltitude |
| 0x44 | 24 | heure UTC : h/1, m/1, (s × 1000 + ms)/1000 | GPSTimeStamp |
| 0x5C | 2 | nombre de satellites en texte (« 12 ») | GPSSatellites |
| 0x5E | 2 | statut en texte (« A » valide, « V » invalide) | GPSStatus |
| 0x60 | 2 (+2) | mode de mesure en texte (« 2 » ou « 3 ») | GPSMeasureMode |
| 0x64 | 8 | DOP (rationnel, ex. 310/100) | GPSDOP |
| 0x6C | 8 | système géodésique en texte (« WGS-84 ») | GPSMapDatum |
| 0x74 | 11 | date UTC « AAAA:MM:JJ » + zéro | GPSDateStamp |
| 0x7F | 1 | 0 (bourrage) | – |

**Format des angles** (« degrés, minutes décimales », comme le NMEA) : degrés/1, (minutes × 10000 + fraction)/10000, 0/1. Exemple : 43,6274917° = 43/1, 376495/10000, 0/1. Le boîtier vérifie que les dénominateurs ne sont pas nuls et que la latitude ne dépasse pas 90°.

Le mode de mesure (0x60) n'est écrit que dans l'EXIF : l'affichage 2D/3D du boîtier vient de 0x91DA. La mise à l'heure automatique du boîtier (réglage « Auto ») utilise l'heure et la date UTC de cette structure.

## Satellites, batterie, boussole

**0x91DA, réception** : `[u32 16][u32 statut][u32 niveau][u32 état]`.

| Champ | Effet sur le boîtier |
| --- | --- |
| niveau | nombre de barres de réception, 0 à 3 (4 et plus = 0 barre) |
| état, bits 0–1 | type de position : 0 aucune, 1 2D sans altitude, 2 3D, 3 2D avec altitude |
| état, bit 2 (0x4) | heure valide : rend disponible la commande « Appliquer » (mise à l'heure) |
| statut | aucun effet visible (valeurs 0 à 8 essayées) |

Valeurs typiques d'un récepteur en 3D avec heure valide : statut 1, niveau 2 ou 3, état 6.

**0x91DE, batterie** : un paramètre, le niveau de batterie du GP-E2 (2 = valeur par défaut du boîtier). L'échelle exacte n'est pas confirmée.

**0x91DD, direction** : `[u32 16][u8 référence][u8 inconnu][u16 0][u32 numérateur][u32 dénominateur]`. Référence en ASCII : « M » nord magnétique, « T » nord géographique ; direction en degrés = numérateur / dénominateur (ex. 19000/100). Pris en compte seulement si la boussole est activée dans le menu GPS du boîtier ; correspond à GPSImgDirectionRef / GPSImgDirection dans l'EXIF.

## Mise à l'heure de l'appareil photo

La mise à l'heure forcée (« Appliquer » dans le menu GPS) se fait en quatre messages ; elle n'est proposée que si l'état satellites a le bit 0x4.

1. **Appareil → GP-E2** : l'utilisateur valide l'heure affichée ; le boîtier poste **0xC1ED** et affiche une boîte d'attente (délai maximal d'environ 60 s).
2. **GP-E2 → appareil** : **0x91DB** avec `[u32 48][struct tm]`, l'heure UTC de la seconde en cours.
3. **Appareil** : il ajoute 1 s, écrit son horloge dès la réception, répond 0x2001 après environ 2 s, puis poste **0xC1EC** avec la valeur 5.
4. **GP-E2 → appareil** : **0x91D8**, paramètre **3**, au début de la seconde suivante (comme une impulsion de seconde). La boîte d'attente se ferme : mise à l'heure réussie.

Sans l'étape 4, le boîtier reste bloqué sur la boîte d'attente.

**struct tm** : 11 entiers signés de 32 bits, petit-boutistes.

| Index | Champ | Exemple (19/10/2026 20:27:17 UTC) |
| --- | --- | --- |
| 0 | secondes (0–59) | 17 |
| 1 | minutes | 27 |
| 2 | heures | 20 |
| 3 | jour du mois (1–31) | 19 |
| 4 | mois (**0–11**) | 9 |
| 5 | année − 1900 | 126 |
| 6 | jour de la semaine (0 = dimanche) | 1 |
| 7 | jour de l'année (0–365) | 291 |
| 8 | heure d'été | 0 |
| 9 | décalage UTC | 0 |
| 10 | fuseau | 0 |

**Précision** : comme le boîtier ajoute 1 s dès la réception, il faut envoyer l'heure de la seconde en cours juste avant la seconde suivante (vers 900 ms). Envoyée à un instant quelconque, l'horloge du boîtier se retrouve en avance d'une fraction de seconde jusqu'à 1 s.

**Attention** : 0x91DB doit respecter exactement ce format. Des données plus courtes (par exemple une heure Unix sur 4 octets) **bloquent complètement le boîtier**, qu'il faut alors débloquer en retirant la batterie.

## Incertitudes restantes

Le côté PC est entièrement décrit, à part 0x91A5, 0x91A6, la propriété 0xD14E (600), l'octet 24 et le bit 1 des points. Côté appareil photo, ce qui reste ouvert :

| Point | État |
| --- | --- |
| Paramètre exact de SetRemoteMode | 9 fonctionne ; la valeur réellement envoyée par le GP-E2 peut avoir d'autres bits |
| Ordre et rythme exacts des envois | rythme de 15 s par défaut puis l'intervalle choisi ; l'ordre 0x91D9 / 0x91DA / 0x91DE est supposé |
| 0x91D8 | seul le paramètre 3 est confirmé ; les valeurs 1, 2 et 6 sont acceptées (1 provoque 0xC1EC avec la valeur 4 ; 6 signalerait un échec de mise à l'heure), usage réel inconnu |
| Instant d'envoi de 0x91DB | déduit du +1 s du boîtier, à confirmer |
| Champ « statut » de 0x91DA | sans effet visible |
| 0x91DE | échelle du niveau de batterie |
| 0x91DD | octet après la référence ; affichage de la direction à vérifier |
| 0x91DC | contenu attendu inconnu |
| Événements 0xC1A4, 0xC1DD, 0xC18E, 0xC1EE | rôle inconnu ; aucune réponse nécessaire |
| Connexion par la griffe flash | non décrite ici ; la connexion USB est la seule documentée |
| Autres boîtiers | vérifié sur EOS 700D (firmware 1.1.5) seulement ; retours bienvenus |
