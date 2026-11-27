"""Publish an approved Instagram carousel and optional Story from a job manifest."""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from zipfile import ZIP_DEFLATED, ZipFile


class PublishError(RuntimeError):
    pass


def graph_call(version, token, path, *, params=None, form=None, host="graph.instagram.com"):
    url = f"https://{host}/{version}/{path}"
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
    media_urls = list(urls)
    if "story_image_url" in job:
        media_urls.append(job["story_image_url"])
    for url in media_urls:
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


def download_archive_images(urls):
    images = []
    max_bytes = 8_000_000
    for index, url in enumerate(urls, 1):
        try:
            with urlopen(url, timeout=25) as response:
                image = response.read(max_bytes + 1)
        except (URLError, TimeoutError, OSError, ValueError):
            raise PublishError(f"Impossible de sauvegarder la carte {index}.") from None
        if len(image) > max_bytes or not image.startswith(b"\xff\xd8\xff"):
            raise PublishError(f"La carte {index} ne correspond pas à un JPEG admissible.")
        images.append(image)
    return images


def write_archive(archive_dir, images, caption, media_id, permalink):
    date = datetime.now(timezone.utc).isoformat()
    destination = archive_dir / f"Carrousel_Instagram_{media_id}.zip"
    with NamedTemporaryFile(dir=archive_dir, prefix=".carrousel-") as temporary:
        with ZipFile(temporary, "w", ZIP_DEFLATED) as archive:
            for index, image in enumerate(images, 1):
                archive.writestr(f"Carte_{index:02d}.jpg", image)
            archive.writestr("Legende_Instagram.txt", caption)
            archive.writestr(
                "Publication.json",
                json.dumps({"media_id": media_id, "permalink": permalink, "archived_at": date},
                           ensure_ascii=False, indent=2) + "\n",
            )
        temporary.flush()
        os.fsync(temporary.fileno())
        os.link(temporary.name, destination)
    return destination


def publish_carousel(job, account, call, receipt_path):
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

    return publish_container(account, call, carousel, receipt_path)


def publish_container(account, call, container_id, receipt_path):
    # Reserve the publication before the irreversible call, including with concurrent workers.
    with receipt_path.open("x", encoding="utf-8") as receipt:
        state = {"container_id": container_id, "status": "publication_uncertain"}
        json.dump(state, receipt)
        receipt.write("\n")
        receipt.flush()
        os.fsync(receipt.fileno())
        media = object_id(call(f"{account}/media_publish", form={"creation_id": container_id}))
        state = {"container_id": container_id, "status": "published", "media_id": media}
        json.dump(state, receipt)
        receipt.write("\n")
        receipt.flush()
        os.fsync(receipt.fileno())
    return state


def publish(job, account, call, receipt_path, *, archive_dir=None):
    validate_job(job)
    if receipt_path.exists():
        raise PublishError("Ce reçu existe déjà. Vérifier le résultat avant toute relance.")
    story_url = job.get("story_image_url")
    story_receipt = receipt_path.with_name(receipt_path.name + ".story")
    if story_url and story_receipt.exists():
        raise PublishError("Le reçu de story existe déjà. Vérifier le résultat avant toute relance.")

    if archive_dir is not None:
        archive_dir.mkdir(parents=True, exist_ok=True)
        images = download_archive_images(job["image_urls"])
    state = publish_carousel(job, account, call, receipt_path)
    media = state["media_id"]
    permalink = None
    if archive_dir is not None:
        try:
            permalink = call(media, params={"fields": "permalink"}).get("permalink")
        except PublishError as exc:
            print(f"Carrousel {media} publié. Lien à vérifier. {exc}", file=sys.stderr)
        try:
            archive_path = write_archive(archive_dir, images, job.get("caption", ""), media, permalink)
            state["archive"] = str(archive_path)
        except OSError as exc:
            print(f"Carrousel {media} publié. Archive en échec. {exc}", file=sys.stderr)
            state["archive_error"] = str(exc)
    if story_url:
        try:
            story = object_id(call(f"{account}/media", form={
                "media_type": "STORIES", "image_url": story_url,
            }))
            wait_for_container(call, story)
            state["story"] = publish_container(account, call, story, story_receipt)
        except (PublishError, OSError) as exc:
            raise PublishError(f"Carrousel {media} publié. Story à vérifier. {exc}") from None

    if archive_dir is None:
        permalink = call(media, params={"fields": "permalink"}).get("permalink")
    return dict(state, permalink=permalink)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, nargs="?")
    parser.add_argument("--check-account", action="store_true")
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--archive-dir", type=Path, help="Dossier durable des carrousels publiés")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        if args.check_account:
            version = os.environ.get("INSTAGRAM_API_VERSION", "")
            token = os.environ.get("INSTAGRAM_ACCESS_TOKEN", "")
            if not token or not re.fullmatch(r"v[0-9]+\.[0-9]+", version):
                raise PublishError("Le jeton Instagram et la version API sont requis.")
            account = graph_call(version, token, "me", params={"fields": "user_id,username"})
            account_id = str(account.get("user_id", ""))
            username = account.get("username")
            if not re.fullmatch(r"[0-9]+", account_id) or not isinstance(username, str) or not username:
                raise PublishError("Meta n’a pas retourné le compte Instagram attendu.")
            print(json.dumps({"account_id": account_id, "username": username}))
            return 0
        if args.manifest is None:
            raise PublishError("Un manifeste est requis pour publier ou simuler.")
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
        host = os.environ.get("INSTAGRAM_GRAPH_HOST", "graph.instagram.com")
        if not token or not re.fullmatch(r"[0-9]+", account) or not re.fullmatch(r"v[0-9]+\.[0-9]+", version):
            raise PublishError("La configuration Instagram est incomplète.")
        if host not in {"graph.instagram.com", "graph.facebook.com"}:
            raise PublishError("Hôte Meta invalide.")

        def call(path, **kwargs):
            return graph_call(version, token, path, host=host, **kwargs)

        result = publish(job, account, call, args.receipt, archive_dir=args.archive_dir)
        print(json.dumps(result))
        return int("archive_error" in result)
    except (PublishError, OSError, ValueError) as exc:
        print(f"Publication interrompue. {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
