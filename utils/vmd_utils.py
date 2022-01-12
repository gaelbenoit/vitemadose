import re
import csv
import threading
import json
import logging
from typing import List, Optional
from urllib.parse import urlparse, urlencode, urlunparse, parse_qs, unquote
import datetime as dt
import pytz
import requests
import sys

from datetime import datetime, timedelta

from unidecode import unidecode

from utils.vmd_config import get_conf_inputs, get_config


RESERVED_CENTERS = get_config().get("reserved_centers", [])

PARIS_TZ = pytz.timezone("Europe/Paris")


def load_insee() -> dict:
    with open(get_conf_inputs().get("from_main_branch").get("postalcode_to_insee")) as json_file:
        return json.load(json_file)


def load_cedex_to_insee() -> dict:
    with open(get_conf_inputs().get("from_main_branch").get("cedex_to_insee")) as json_file:
        return json.load(json_file)


def get_departements(excluded_departments: List[str] = []) -> List[str]:
    with open(get_conf_inputs()["from_main_branch"]["departements"], encoding="utf8", newline="\n") as csvfile:
        reader = csv.DictReader(csvfile)
        departements = [row["nom_departement"] for row in reader if row["nom_departement"] not in excluded_departments]
        return departements


def get_departements_numbers(excluded_departments: List[str] = []) -> List[str]:
    with open(get_conf_inputs()["from_main_branch"]["departements"], encoding="utf8", newline="\n") as csvfile:
        reader = csv.DictReader(csvfile)
        departements = [row["code_departement"] for row in reader if row["nom_departement"] not in excluded_departments]
        return departements


logger = logging.getLogger("scraper")
insee = load_insee()
cedex_to_insee = load_cedex_to_insee()


def is_reserved_center(center):
    if not center:
        return False
    if not center.nom:
        return False
    if center.nom:
        name = center.nom.lower().strip()
    for reserved_names in RESERVED_CENTERS:
        if reserved_names in name:
            return True
    return False


def urlify(s):
    s = re.sub(r"[^\w\s\-]", "", s)
    s = re.sub(r"\s+", "-", s).lower()
    return unidecode(s)


def department_urlify(department: str) -> str:
    department = re.sub(r"\s+|\W", "-", department).lower()
    return unidecode(department)


class departementUtils:
    @staticmethod
    def import_departements() -> List[str]:
        """
        Renvoie la liste des codes départements.

        >>> departements = import_departements()
        >>> len(departements)
        101
        >>> departements[:3]
        ['01', '02', '03']
        >>> departements[83]
        '83'
        >>> departements.index('2A')
        28
        >>> sorted(departements) == departements
        True
        """
        with open(get_conf_inputs().get("from_main_branch").get("departements"), newline="\n") as csvfile:
            reader = csv.DictReader(csvfile)
            return [str(row["code_departement"]) for row in reader]

    INSEE_TO_POSTALCODE_AND_DEP = None

    @staticmethod
    def to_departement_number(insee_code: str) -> str:
        """
        Renvoie le numéro de département correspondant au code INSEE d'une commune.

        Le code INSEE est un code à 5 chiffres, qui est typiquement différent du code postal,
        mais qui commence (en général) aussi par les 2 chiffres du département.

        >>> to_departement_number('59350')  # Lille
        '59'
        >>> to_departement_number('75106')  # Paris 6e arr
        '75'
        >>> to_departement_number('97701')  # Saint-Barthélémy
        '971'
        """
        if not insee_code:
            logger.warning("Null insee code")
            return None
        insee_code = insee_code.strip()
        if len(insee_code) == 4:
            # Quand le CSV des centres de vaccinations est édité avec un tableur comme Excel,
            # il est possible que le 1er zéro soit retiré si la colonne est interprétée comme
            # un nombre (par ex 02401 devient 2401, mais on veut 02401 au complet).
            insee_code = insee_code.zfill(5)

        if len(insee_code) != 5:
            raise ValueError(f"Code INSEE non-valide : {insee_code}")

        if departementUtils.INSEE_TO_POSTALCODE_AND_DEP is None:
            with open(get_conf_inputs().get("from_main_branch").get("insee_to_postalcode_and_dep")) as json_file:
                departementUtils.INSEE_TO_POSTALCODE_AND_DEP = json.load(json_file)

        if insee_code in departementUtils.INSEE_TO_POSTALCODE_AND_DEP:
            return departementUtils.INSEE_TO_POSTALCODE_AND_DEP[insee_code]["departement"]
        else:
            raise ValueError(f"Code INSEE absent de la base des codes INSEE : {insee_code}")

    @staticmethod
    def get_city(address: str) -> Optional[str]:
        """
        Récupère la ville depuis l'adresse complète
        >>> get_city("2 avenue de la République, 75005 PARIS")
        'PARIS'
        """
        if not address:
            return None
        # tmp debug
        if search := re.search(r"\D+\d{5}\s+(?P<com_nom>.*\S)\s*$", address):
            return search.groupdict().get("com_nom")
        return None

    @staticmethod
    def get_cp(address: str) -> Optional[str]:
        """
        Récupère le code postal depuis l'adresse complète
        >>> get_cp(("2 avenue de la République, 75005 PARIS")
        '75005'
        """
        if not address:
            return None
        if search := re.search(r"\b\d{5}\b", address):
            return search.group(0)
        return None

    @staticmethod
    def cp_to_insee(cp: str) -> str:
        # Split for when when CP is like 'XXXX CEDEX'
        if not isinstance(cp, str):
            cp = str(cp)
        cp = format_cp(cp)
        if cp in insee:
            return insee[cp]["insee"]
        elif cp in cedex_to_insee:
            cedex = cp
            return cedex_to_insee[cedex]["insee"]
        else:
            logger.warning(f"Unable to translate cp >{cp}< to insee")
            return cp


def format_cp(cp: str) -> str:
    # Permet le cas du CP sous form 75 005 au lieu de 75005
    formatted_cp = cp
    if len(re.findall(r"\d+", cp)) > 0:
        formatted_cp = re.findall(r"\d+", cp)[0]
    else:
        logger.warning(f"postcode {cp} is incorrect")
    if len(formatted_cp) == 4:
        return f"0{formatted_cp}"
    return formatted_cp


def format_phone_number(_phone_number: str) -> str:
    phone_number = _phone_number
    if not phone_number:
        return ""

    phone_number = phone_number.replace(" ", "")
    phone_number = phone_number.replace(".", "")
    if not len(phone_number) > 0:
        return ""
    if not phone_number[0] == "+":
        if phone_number[:2] == "00":
            phone_number = "+" + phone_number[2:]
        elif phone_number[0] == "0":
            phone_number = "+33" + phone_number[1:]
        else:
            phone_number = "+33" + phone_number

    return phone_number


def fix_scrap_urls(url):
    url = unquote(url.strip())

    # Fix Keldoc
    if url.startswith("https://www.keldoc.com/"):
        url = url.replace("https://www.keldoc.com/", "https://vaccination-covid.keldoc.com/")
    # Clean Doctolib
    if url.startswith("https://partners.doctolib.fr") or url.startswith("https://www.doctolib.fr"):
        if "?speciality_id" in url:
            url = "&".join(url.rsplit("?", url.count("?") - 1))
        u = urlparse(url)
        query = parse_qs(u.query, keep_blank_values=True)
        to_remove = []
        for query_name in query:
            if query_name.startswith("highlight") or query_name == "enable_cookies_consent":
                to_remove.append(query_name)
        [query.pop(rm, None) for rm in to_remove]
        query.pop("speciality_id", None)
        u = u._replace(query=urlencode(query, True))
        url = urlunparse(u)
    return url


def get_last_scans(centres):
    url = f'{get_config().get("base_urls").get("gitlab_public_path")}/{get_conf_inputs().get("from_gitlab_public").get("last_scans")}'
    last_scans = {}
    liste_centres = []

    for centre in centres:
        liste_centres.append(centre)

    try:
        response = requests.get(url)
        response.raise_for_status()
        info_centres = response.json()

    except Exception as e:
        logger.warning(f"Impossible de récupérer le fichier info_centres: {e}")
        info_centres = {}

    for centre in info_centres["centres_disponibles"] + info_centres["centres_indisponibles"]:
        if "last_scan_with_availabilities" in centre:
            last_scans[centre["url"]] = centre["last_scan_with_availabilities"]

    for centre in liste_centres:
        if not centre.prochain_rdv:
            if centre.url in last_scans:
                centre.last_scan_with_availabilities = last_scans[centre.url]
        else:
            centre.last_scan_with_availabilities = dt.datetime.now(tz=pytz.timezone("Europe/Paris")).isoformat()
    return liste_centres


def append_date_days(mydate: str, days: int, seconds=0):
    if not mydate:
        return

    mydate = datetime.fromisoformat(mydate)
    newdate = mydate + timedelta(days=days, seconds=seconds)
    newdate = PARIS_TZ.localize(newdate)

    return newdate.isoformat()


def get_start_date():
    return dt.date.today().isoformat()


class DummyQueue:
    def __init__(self):
        pass

    def put(self, *args, **kwargs):
        pass


# Special value to Signal end of Queue content
# This needs to be serializable in order to be used accross processes
EOQ = "EOQ-f43732d8-c250-11eb-8d1f-f38a886756c1"


def q_iter(q, EOQ=EOQ):
    return iter(q.get, EOQ)


class BulkQueue:
    def __init__(self, q, bulksize=300, delay=5):
        self.q = q
        self.bulksize = bulksize
        self.current_read = None
        self.current_bulk = []
        self._scheduled_timer = None
        self.delay = delay

    def put(self, item):
        self.current_bulk.append(item)
        if len(self.current_bulk) >= self.bulksize:
            self._flush()
            return None

        if self._scheduled_timer:
            self._scheduled_timer.cancel()

        self._scheduled_timer = threading.Timer(self.delay, self._flush)
        self._scheduled_timer.start()

    def get(self):
        if not self.current_read:
            next_bulk = self.q.get()
            self.current_read = iter(next_bulk)

        try:
            return next(self.current_read)
        except StopIteration:
            self.current_read = None
            return self.get()

    def delayed_flush(self):
        self._flush()

    def _flush(self):
        if not self.current_bulk or len(self.current_bulk) == 0:
            return
        bulk = self.current_bulk
        self.current_bulk = []
        self.q.put(bulk)
