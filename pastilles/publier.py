import json
import os
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ACCOUNT_ID = "17841444020904801"
USERNAME = "lafrise.fr"
CAPTION = "Certaines pastilles au miel ou au menthol sont de retour en pharmacie, le temps d’écouler les stocks jusqu’au 16 mai 2027 inclus.\n\nEn 1900, un pharmacien parisien dépose la marque Valda. En 2002, la confiserie pharmaceutique figure dans la liste des produits autorisés en officine. En novembre 2026, cette catégorie disparaît de la liste. Les pastilles qui relèvent d’une catégorie toujours autorisée peuvent rester en pharmacie.\n\nOn se retrouve à la prochaine frise. Abonne-toi.\n\nSources\nCartes 1 et 6 — Fédération des pharmaciens de France\nCartes 2 et 3 — Musée des Arts décoratifs, histoire publicitaire de Valda\nCarte 2 — Société d’Histoire de la Pharmacie\nCarte 4 — Légifrance, arrêté du 15 février 2002\nCarte 5 — Légifrance, arrêté du 4 novembre 2026\n\nIllustrations de reconstitution.\n\n#LaFrise #Pastilles #Pharmacie #Valda #HistoireDuQuotidien"
BASE = "https://raw.githubusercontent.com/jflevere/lafrise_generation-service/"
GRAPH = "https://graph.facebook.com/v27.0/"

def meta(path, token, payload=None):
    request = Request(
        GRAPH + path,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=25) as response:
            return json.load(response)
    except HTTPError as error:
        try:
            details = json.load(error).get("error", {})
        except ValueError:
            details = {}
        raise SystemExit(f"Meta HTTP {error.code}, code {details.get('code')}, sous-code {details.get('error_subcode')}") from None
    except (URLError, TimeoutError, ValueError):
        raise SystemExit("Réponse Meta incertaine. Vérifie la grille avant toute relance.") from None

def numeric_id(response, title):
    value = str(response.get("id", ""))
    if not re.fullmatch(r"[0-9]+", value):
        raise SystemExit(f"Meta n’a pas confirmé l’identifiant {title}.")
    return value

def ready(container_id, token):
    for attempt in range(10):
        status = meta(container_id + "?fields=status_code", token).get("status_code")
        if status == "FINISHED":
            return
        if status != "IN_PROGRESS":
            raise SystemExit(f"Conteneur {container_id}, statut Meta {status}. Publication interrompue.")
        if attempt < 9:
            time.sleep(5)
    raise SystemExit(f"Conteneur {container_id} encore en traitement.")

def publish(commit, token):
    if meta(ACCOUNT_ID + "?fields=username", token).get("username") != USERNAME:
        raise SystemExit("Le compte renvoyé ne correspond pas à @lafrise.fr.")
    recent = meta(ACCOUNT_ID + "/media?fields=id,caption,permalink,media_type&limit=25", token)
    if not isinstance(recent.get("data"), list):
        raise SystemExit("Impossible de lire les dernières publications.")
    signature = CAPTION.splitlines()[0]
    for item in recent["data"]:
        if (item.get("caption") or "").startswith(signature):
            print("Publication correspondante déjà présente :", item.get("permalink") or item.get("id"))
            return
    children = []
    for index in range(1, 7):
        child = meta(ACCOUNT_ID + "/media", token, {
            "image_url": f"{BASE}{commit}/pastilles/La_Frise_Pastilles_{index:02d}.jpg",
            "is_carousel_item": True,
        })
        children.append(numeric_id(child, f"de la carte {index}"))
        print(f"Carte {index}/6 reçue par Meta.", flush=True)
    for child_id in children:
        ready(child_id, token)
    parent = meta(ACCOUNT_ID + "/media", token, {
        "media_type": "CAROUSEL", "children": ",".join(children), "caption": CAPTION,
    })
    parent_id = numeric_id(parent, "du carrousel")
    print("Conteneur du carrousel :", parent_id, flush=True)
    ready(parent_id, token)
    result = meta(ACCOUNT_ID + "/media_publish", token, {"creation_id": parent_id})
    media_id = numeric_id(result, "de la publication")
    print("Publication acceptée par Meta. ID :", media_id, flush=True)
    post = meta(media_id + "?fields=permalink,media_type,children{id}", token)
    print("Lien :", post.get("permalink") or "à récupérer")
    count = len((post.get("children") or {}).get("data", []))
    print(f"Média Meta : {post.get('media_type')}, {count} cartes renvoyées.")

if __name__ == "__main__":
    token = os.environ.get("INSTAGRAM_ACCESS_TOKEN", "").strip()
    if len(sys.argv) != 2 or not re.fullmatch(r"[0-9a-f]{40}", sys.argv[1]) or not token:
        raise SystemExit("Jeton Render absent ou version du fichier invalide.")
    if re.search(r"https?://|www\.", CAPTION, re.I):
        raise SystemExit("Un lien figure dans la légende. Publication interrompue.")
    publish(sys.argv[1], token)
