import os

import httpx
import json
import logging

from datetime import date, datetime, timedelta
from dateutil.parser import isoparse, parse as dateparse
from pytz import timezone
from pathlib import Path
from urllib import parse
from typing import Optional

from scraper.pattern.scraper_request import ScraperRequest
from scraper.pattern.scraper_result import DRUG_STORE
from scraper.pattern.vaccine import get_vaccine_name
from utils.vmd_config import get_conf_platform, get_config
from scraper.profiler import Profiling
from scraper.creneaux.creneau import Creneau, Lieu, Plateforme, PasDeCreneau
from utils.vmd_utils import departementUtils, DummyQueue

MAPHARMA_CONF = get_conf_platform("mapharma")
MAPHARMA_API = MAPHARMA_CONF.get("api", {})
MAPHARMA_ENABLED = MAPHARMA_CONF.get("enabled", False)

# timeout = httpx.Timeout(MAPHARMA_CONF.get("timeout", 25), connect=MAPHARMA_CONF.get("timeout", 25))

MAPARMA_REFERER = MAPHARMA_CONF.get("headers", {}).get("referer", {})
MAPHARMA_HEADERS = {"User-Agent": os.environ.get("MAPHARMA_API_KEY", ""), "Referer": MAPARMA_REFERER}

MAPHARMA_FILTERS = MAPHARMA_CONF.get("filters", {})
MAPHARMA_CAMPAGNES_VALIDES = MAPHARMA_CONF.get("valid_campaigns", [])
MAPHARMA_CAMPAGNES_INVALIDES = MAPHARMA_CONF.get("invalid_campaigns", [])

MAPHARMA_PATHS = MAPHARMA_CONF.get("paths", {})
MAPHARMA_OPEN_DATA_FILE = Path(MAPHARMA_PATHS.get("opendata", ""))
MAPHARMA_OPEN_DATA_URL = MAPHARMA_API.get("opendata", "")
MAPHARMA_OPEN_DATA_URL_FALLBACK = MAPHARMA_API.get("opendata_fallback", "")
NUMBER_OF_SCRAPED_DAYS = get_config().get("scrape_on_n_days", 28)

BOOSTER_VACCINES = get_config().get("vaccines_allowed_for_booster", [])

DEFAULT_CLIENT = httpx.Client(headers=MAPHARMA_HEADERS)
logger = logging.getLogger("scraper")
paris_tz = timezone("Europe/Paris")

campagnes_valides = []
campagnes_inconnues = []
opendata = []


def get_possible_dose_numbers(vaccine_list: list):
    if not vaccine_list:
        return []
    if any([vaccine in BOOSTER_VACCINES for vaccine in vaccine_list]):
        return [1, 2, 3]
    return [1, 2]


@Profiling.measure("mapharma_slot")
def fetch_slots(
    request: ScraperRequest,
    creneau_q=DummyQueue(),
    client: httpx.Client = DEFAULT_CLIENT,
    opendata_file: str = MAPHARMA_OPEN_DATA_FILE,
) -> Optional[str]:

    if not MAPHARMA_ENABLED:
        return None
    # Fonction principale avec le comportement "de prod".
    mapharma = Mapharma(client=DEFAULT_CLIENT, creneau_q=creneau_q, opendata_file=opendata_file)
    return mapharma.fetch(request, client)


def get_mapharma_opendata(
    client: httpx.Client = DEFAULT_CLIENT,
    opendata_url: str = MAPHARMA_OPEN_DATA_URL,
    opendata_url_fallback: str = MAPHARMA_OPEN_DATA_URL_FALLBACK,
) -> dict:
    try:
        request = client.get(opendata_url, headers=MAPHARMA_HEADERS)
        request.raise_for_status()

        # Let's update opendata file
        f = open(MAPHARMA_OPEN_DATA_FILE, "w", encoding="utf-8")
        f.write(
            json.dumps(
                {"artifact_date": datetime.today().strftime("%Y-%m-%d %H:%M:%S"), "data": request.json()}, indent=2
            )
        )
        f.close()

        return request.json()

    except httpx.TimeoutException as hex:
        logger.warning(f"{opendata_url} timed out {hex}")
    except httpx.HTTPStatusError as hex:
        logger.warning(f"{opendata_url} returned error {hex.response.status_code}")
    try:
        request = client.get(opendata_url_fallback, headers=MAPHARMA_HEADERS)
        request.raise_for_status()
        return request.json()["data"]
    except httpx.TimeoutException as hex:
        logger.warning(f"{opendata_url_fallback} timed out {hex}")
    except httpx.HTTPStatusError as hex:
        logger.warning(f"{opendata_url_fallback} returned error {hex.response.status_code}")
    return None


def campagne_to_centre(pharmacy: dict, campagne: dict) -> dict:
    if not pharmacy.get("code_postal"):
        raise ValueError("Absence de code postal")
    insee = departementUtils.cp_to_insee(pharmacy.get("code_postal"))
    centre = dict()
    centre["nom"] = pharmacy.get("nom")
    centre["type"] = DRUG_STORE
    centre["long_coor1"] = pharmacy.get("longitude")
    centre["lat_coor1"] = pharmacy.get("latitude")
    centre["com_nom"] = pharmacy.get("ville")
    adr_voie = pharmacy.get("adresse")
    adr_cp = pharmacy.get("code_postal")
    adr_nom = pharmacy.get("ville")
    centre["com_cp"] = adr_cp
    centre["address"] = f"{adr_voie}, {adr_cp} {adr_nom}"
    business_hours = dict()
    horaires = pharmacy.get("horaires", "")
    days = MAPHARMA_CONF.get("business_days", [])
    for day in days:
        for line in horaires.splitlines():
            if day not in line:
                continue
            business_hours[day] = line.replace(f"{day}: ", "")
    centre["business_hours"] = business_hours
    centre["phone_number"] = pharmacy.get("telephone", "")
    centre["rdv_site_web"] = campagne.get("url")
    centre["com_insee"] = insee
    centre["gid"] = campagne.get("url").encode("utf8").hex()[52:][:23]
    return centre


class Mapharma:
    def __init__(
        self, opendata_file=MAPHARMA_OPEN_DATA_FILE, creneau_q=DummyQueue, client: httpx.Client = DEFAULT_CLIENT
    ):
        self.creneau_q = creneau_q
        self.lieu = None
        self.opendata_file = opendata_file

    def found_creneau(self, creneau):
        self.creneau_q.put(creneau)

    @Profiling.measure("mapharma_opendata")
    def get_pharmacy_and_campagne(
        self,
        id_campagne: int,
        id_type: int,
    ) -> [dict, dict]:
        opendata = list()
        try:
            with open(self.opendata_file, "r", encoding="utf8") as f:
                opendata = json.load(f)["data"]
        except IOError as ioex:
            logger.warning(f"Reading {self.opendata_file} returned error {ioex}")
        for pharmacy in opendata:
            for campagne in pharmacy["campagnes"]:
                if id_campagne == campagne["id_campagne"] and id_type == campagne["id_type"]:
                    return pharmacy, campagne
        raise ValueError(f"Unable to find campagne (c={id_campagne}&l={id_type})")

    def get_slots(
        self,
        campagneId: str,
        optionId: str,
        start_date: str,
        client: httpx.Client = DEFAULT_CLIENT,
        request: ScraperRequest = None,
    ) -> dict:

        base_url = MAPHARMA_API.get("slots").format(campagneId=campagneId, start_date=start_date, optionId=optionId)
        if request:
            request.increase_request_count("slots")
        try:
            r = client.get(base_url)
            r.raise_for_status()
        except httpx.TimeoutException as hex:
            logger.warning(f"{base_url} timed out {hex}")
            request.increase_request_count("time-out")
            return {}
        except httpx.HTTPStatusError as hex:
            logger.warning(f"{base_url} returned error {hex.response.status_code}")
            request.increase_request_count("error")
            return {}
        return r.json()

    def parse_slots(self, slots, start_date, request, vaccine) -> [datetime, int]:
        first_availability = None
        slot_count = 0

        for day, day_slots in slots.items():
            if "first" not in day and date.fromisoformat(day) >= start_date:
                for day_slot in day_slots:
                    time = day_slot["time"]
                    timestamp = datetime.strptime(f"{day} {time}", "%Y-%m-%d %H:%M")
                    slot_count += day_slot["places_dispo"]
                    for appointment in range(1, day_slot["places_dispo"] + 1):
                        dose_ranks = get_possible_dose_numbers([vaccine])

                        self.found_creneau(
                            Creneau(
                                horaire=paris_tz.localize(timestamp),
                                reservation_url=request.url,
                                dose=dose_ranks,
                                type_vaccin=[vaccine],
                                lieu=self.lieu,
                            )
                        )

                    if first_availability is None or timestamp < first_availability:
                        first_availability = timestamp
        return first_availability, slot_count

    def count_appointements(self, slots: dict, start_date: datetime, end_date: datetime) -> int:
        count = 0

        for day, day_slots in slots.items():
            day_date = paris_tz.localize(isoparse(day) + timedelta(days=0))
            if day_date >= start_date and day_date < end_date:
                count += len(day_slots)

        logger.debug(f"Slots count from {start_date.isoformat()} to {end_date.isoformat()}: {count}")
        return count

    def fetch(self, request, client):

        self.lieu = Lieu(
            plateforme=Plateforme.MAPHARMA,
            url=request.url,
            location=request.center_info.location,
            nom=request.center_info.nom,
            internal_id=f"mapharma{request.internal_id}",
            departement=request.center_info.departement,
            lieu_type=request.practitioner_type,
            metadata=request.center_info.metadata,
        )

        url = request.get_url()
        # on récupère les paramètres c (id_campagne) & l (id_type)
        params = dict(parse.parse_qsl(parse.urlsplit(url).query))
        id_campagne = int(params.get("c"))
        id_type = int(params.get("l"))
        day_slots = {}
        # certaines campagnes ont des dispos mais 0 doses
        # si total_libres est à 0 c'est qu'il n'y a pas de vraies dispo
        pharmacy, campagne = self.get_pharmacy_and_campagne(id_campagne, id_type)
        if campagne is None or campagne["total_libres"] == 0:
            return None
        # l'api ne renvoie que 7 jours, on parse un peu plus loin dans le temps
        start_date = date.fromisoformat(request.get_start_date())
        for delta in range(0, NUMBER_OF_SCRAPED_DAYS, 6):
            new_date = start_date + timedelta(days=delta)
            slots = self.get_slots(id_campagne, id_type, new_date.isoformat(), client, request=request)
            for day, day_slot in slots.items():
                if day in day_slots:
                    continue
                day_slots[day] = day_slot
        if not day_slots:
            return
        day_slots.pop("first", None)
        day_slots.pop("first_text", None)

        first_availability, slot_count = self.parse_slots(
            day_slots, start_date, request, get_vaccine_name(campagne["nom"])
        )
        request.update_appointment_count(slot_count)
        request.update_practitioner_type(DRUG_STORE)
        request.add_vaccine_type(get_vaccine_name(campagne["nom"]))

        if first_availability is None:
            if self.lieu:
                self.found_creneau(PasDeCreneau(lieu=self.lieu))
            return None
        return first_availability.isoformat()


def is_campagne_valid(campagne: dict) -> bool:
    global campagnes_inconnues
    global campagnes_valides
    if not campagne.get("url"):
        return False
    if "vaccination_covid" in campagne:
        return campagne.get("vaccination_covid")
    if any(keyword in campagne.get("nom", "erreur").lower() for keyword in MAPHARMA_CAMPAGNES_INVALIDES):
        return False
    if any(keyword in campagne.get("nom", "erreur").lower() for keyword in MAPHARMA_CAMPAGNES_VALIDES):
        return True
    if not campagnes_valides:
        # on charge la liste des campagnes valides (vaccination)
        with open(Path(MAPHARMA_PATHS.get("valid_campaigns")), "r", encoding="utf8") as f:
            campagnes_valides = json.load(f)
    if not campagnes_inconnues:
        # on charge la liste des campagnes non valides (tests, ...)
        with open(Path(MAPHARMA_PATHS.get("invalid_campaigns")), "r", encoding="utf8") as f:
            campagnes_inconnues = json.load(f)
    for campagne_valide in campagnes_valides:
        if campagne.get("url") == campagne_valide.get("url"):
            return True
    # la campagne n'existe pas dans la liste des valides, on l'ajoute aux inconnues
    for campagne_inconnue in campagnes_inconnues:
        if campagne.get("url") == campagne_inconnue.get("url"):
            # on a trouvé la campagne inconnue dans la liste
            # pas la peine de l'ajouter
            return False
    campagnes_inconnues.append(campagne)
    return False


def centre_iterator():
    if not MAPHARMA_ENABLED:
        logger.warning("Mapharma scrap is disabled in configuration file.")
        return []
    global opendata
    global campagnes_inconnues
    opendata = get_mapharma_opendata()
    if not opendata:
        logger.error("Mapharma unable to get centre list")
        return

    for pharmacy in opendata:
        for campagne in pharmacy.get("campagnes"):
            if not is_campagne_valid(campagne):
                continue
            centre = campagne_to_centre(pharmacy=pharmacy, campagne=campagne)
            yield centre
    # on sauvegarde la liste des campagnes inconnues pour review
    with open(Path(MAPHARMA_PATHS.get("invalid_campaigns")), "w", encoding="utf8") as f:
        json.dump(campagnes_inconnues, f, indent=2)
