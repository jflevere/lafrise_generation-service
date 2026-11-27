"""Publish an approved Instagram carousel from a job manifest."""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


class PublishError(RuntimeError):
    pass


def graph_call(version, token, path, *, params=None, form=None):
    url = f"https://graph.instagram.com/{version}/{path}"
    if params:
        url += "?" + urlencode(params)
    request = Request(
        url,
        data=urlencode(form).encode() if form is not None else None,
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urlopen(request, timeout=20) as response:
            return json.load(response)
    except HTTPError as exc:
        raise PublishError(f"Meta a répondu HTTP {exc.code} sur {path}") from None
    except (URLError, TimeoutError, ValueError):
        raise PublishError(f"Réponse Meta indisponible ou illisible sur {path}") from None


def validate_job(job):
    urls = job.get("image_urls")
    if job.get("approved") is not True:
        raise PublishError("Le carrousel doit être approuvé.")
    if not isinstance(urls, list) or not 2 <= len(urls) <= 10:
        raise PublishError("Le carrousel doit contenir 2 à 10 images JPEG.")
    for url in urls:
        if not isinstance(url, str):
            raise PublishError("Chaque image doit avoir une URL HTTPS.")
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username:
            raise PublishError("Chaque image doit avoir une URL HTTPS accessible à Meta.")
    if not isinstance(job.get("caption", ""), str):
        raise PublishError("La légende doit être du texte.")


def object_id(response):
    identifier = response.get("id")
    if not isinstance(identifier, str) or not identifier.isascii() or not identifier.isdecimal():
        raise PublishError("Meta n’a pas retourné d’identifiant valide.")
    return identifier


def wait_for_container(call, identifier):
    for attempt in range(6):
        status = call(identifier, params={"fields": "status_code"}).get("status_code")
        if status == "FINISHED":
            return
        if status != "IN_PROGRESS":
            raise PublishError(f"Conteneur Meta indisponible ({status}).")
        if attempt < 5:
            time.sleep(10)
    raise PublishError("Le conteneur Meta est encore en traitement.")


def publish(job, account, call, receipt_path):
    validate_job(job)
    if receipt_path.exists():
        raise PublishError("Ce reçu existe déjà. Vérifier le résultat avant toute relance.")
    children = []
    for url in job["image_urls"]:
        child = object_id(call(f"{account}/media", form={
            "image_url": url, "is_carousel_item": "true",
        }))
        wait_for_container(call, child)
        children.append(child)
    carousel = object_id(call(f"{account}/media", form={
        "media_type": "CAROUSEL",
        "children": ",".join(children),
        "caption": job.get("caption", ""),
    }))
    wait_for_container(call, carousel)

    # Reserve the publication before the irreversible call, including with concurrent workers.
    with receipt_path.open("x", encoding="utf-8") as receipt:
        state = {"container_id": carousel, "status": "publication_uncertain"}
        json.dump(state, receipt)
        receipt.write("\n")
        receipt.flush()
        os.fsync(receipt.fileno())
        media = object_id(call(f"{account}/media_publish", form={"creation_id": carousel}))
        state = {"container_id": carousel, "status": "published", "media_id": media}
        json.dump(state, receipt)
        receipt.write("\n")
        receipt.flush()
        os.fsync(receipt.fileno())
    permalink = call(media, params={"fields": "permalink"}).get("permalink")
    return dict(state, permalink=permalink)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        job = json.loads(args.manifest.read_text(encoding="utf-8"))
        if not isinstance(job, dict):
            raise PublishError("Le manifeste doit être un objet JSON.")
        validate_job(job)
        if args.dry_run:
            print(json.dumps({"status": "validated", "images": len(job["image_urls"])}))
            return 0
        if args.receipt is None:
            raise PublishError("Un chemin de reçu durable est requis pour publier.")
        account = os.environ.get("INSTAGRAM_ACCOUNT_ID", "")
        version = os.environ.get("INSTAGRAM_API_VERSION", "")
        token = os.environ.get("INSTAGRAM_ACCESS_TOKEN", "")
        if not token or not re.fullmatch(r"[0-9]+", account) or not re.fullmatch(r"v[0-9]+\.[0-9]+", version):
            raise PublishError("La configuration Instagram est incomplète.")

        def call(path, **kwargs):
            return graph_call(version, token, path, **kwargs)

        print(json.dumps(publish(job, account, call, args.receipt)))
        return 0
    except (PublishError, OSError, ValueError) as exc:
        print(f"Publication interrompue. {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
