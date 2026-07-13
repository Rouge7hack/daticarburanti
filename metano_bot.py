"""
Bot Telegram - Migliori distributori (metano, benzina, ...) vicino a casa
Dati: CSV ufficiali del MIMIT, scaricati direttamente da GitHub Actions.

Pensato per girare come workflow schedulato su GitHub Actions.
"""

import html
import io
import math
import os
import sys
from datetime import datetime

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ----------------------------------------------------------------------
# CONFIGURAZIONE - modifica questi valori
# ----------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
_HOME_LAT_RAW = os.environ.get("HOME_LAT", "")
_HOME_LON_RAW = os.environ.get("HOME_LON", "")

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID or not _HOME_LAT_RAW or not _HOME_LON_RAW:
    sys.exit(
        "Errore: imposta le variabili d'ambiente TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, "
        "HOME_LAT e HOME_LON (su GitHub: Settings -> Secrets and variables -> Actions)."
    )

HOME_LAT = float(_HOME_LAT_RAW)
HOME_LON = float(_HOME_LON_RAW)

RAGGIO_RICERCA_KM = 15           # entro quanti km cercare i distributori
NUM_RISULTATI = 5                # quanti distributori mostrare per ciascun carburante

# Un blocco per ogni carburante da cercare. "nome_csv" deve corrispondere
# esattamente al valore di descCarburante nel CSV del MIMIT.
CARBURANTI = [
    {
        "nome_csv": "Metano",
        "etichetta": "METANO",
        "unita": "kg",
        "consumo_100km": 3.8,     # kg / 100km
        "tank_size": 10,          # kg assunti per un "pieno" tipico
        "emoji": "⛽",
    },
    {
        "nome_csv": "Benzina",
        "etichetta": "BENZINA",
        "unita": "l",
        "consumo_100km": 7.5,     # litri / 100km
        "tank_size": 50,          # litri del serbatoio
        "emoji": "🚗",
    },
]

URL_PREZZI = "https://www.mimit.gov.it/images/exportCSV/prezzo_alle_8.csv"
URL_ANAGRAFICA = "https://www.mimit.gov.it/images/exportCSV/anagrafica_impianti_attivi.csv"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/csv,application/octet-stream,text/plain,*/*",
    "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://www.mimit.gov.it/it/open-data/elenco-dataset/carburanti-prezzi-praticati-e-anagrafica-degli-impianti",
    "Connection": "keep-alive",
}

_session = requests.Session()
_retry_strategy = Retry(
    total=5,
    backoff_factor=3,          # attese di 3s, 6s, 12s, 24s, 48s tra un tentativo e l'altro
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET"],
)
_session.mount("https://", HTTPAdapter(max_retries=_retry_strategy))

# ----------------------------------------------------------------------
# FUNZIONI
# ----------------------------------------------------------------------

def scarica_csv(url: str) -> pd.DataFrame:
    """Scarica e legge un CSV (separatore a barra verticale, come pubblicato dal MIMIT).

    Attenzione a due particolarità dei file MIMIT:
    1) la prima riga del file spesso è un commento tipo "Estrazione del 2026-07-06"
       e va scartata prima di interpretare l'header vero e proprio;
    2) alcune righe contengono un carattere '|' in più dentro un campo (es.
       nell'indirizzo), che genera righe con un campo di troppo: le saltiamo
       con on_bad_lines='skip' (si perdono pochissimi impianti su migliaia).
    """
    resp = _session.get(url, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    testo = resp.content.decode("latin-1")

    righe = testo.splitlines()
    if righe and righe[0].strip().lower().startswith("estrazione"):
        righe = righe[1:]
    testo_pulito = "\n".join(righe)

    return pd.read_csv(
        io.StringIO(testo_pulito),
        sep="|",
        engine="python",
        on_bad_lines="skip",
    )


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    """Distanza in linea d'aria (km) tra due coordinate GPS."""
    r = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def trova_migliori_distributori(prezzi: pd.DataFrame, anagrafica: pd.DataFrame, carburante: dict) -> pd.DataFrame:
    # Filtra solo il carburante richiesto
    filtrato = prezzi[
        prezzi["descCarburante"].str.strip().str.lower() == carburante["nome_csv"].lower()
    ].copy()

    # Unisci con l'anagrafica per avere indirizzo e coordinate
    df = filtrato.merge(anagrafica, on="idImpianto", how="inner")

    # Scarta impianti senza coordinate valide
    df = df.dropna(subset=["Latitudine", "Longitudine", "prezzo"])
    df["Latitudine"] = pd.to_numeric(df["Latitudine"], errors="coerce")
    df["Longitudine"] = pd.to_numeric(df["Longitudine"], errors="coerce")
    df = df.dropna(subset=["Latitudine", "Longitudine"])

    # Calcola la distanza da casa
    df["distanza_km"] = df.apply(
        lambda r: haversine_km(HOME_LAT, HOME_LON, r["Latitudine"], r["Longitudine"]), axis=1
    )
    df = df[df["distanza_km"] <= RAGGIO_RICERCA_KM]

    if df.empty:
        return df

    # Costo "effettivo" stimato: prezzo unitario + il costo del carburante bruciato
    # per andare e tornare dal distributore, spalmato su un pieno tipico.
    # Questo penalizza i distributori economici ma troppo lontani.
    consumo_100km = carburante["consumo_100km"]
    tank_size = carburante["tank_size"]
    df["costo_deviazione_eur"] = df["distanza_km"] * 2 * (consumo_100km / 100) * df["prezzo"]
    df["prezzo_effettivo"] = df["prezzo"] + (df["costo_deviazione_eur"] / tank_size)

    df = df.sort_values("prezzo_effettivo").head(NUM_RISULTATI)
    return df


def formatta_sezione(df: pd.DataFrame, carburante: dict) -> str:
    unita = carburante["unita"]
    if df.empty:
        return (
            f"{carburante['emoji']} <b>{carburante['etichetta']}</b>\n"
            f"Nessun distributore trovato entro {RAGGIO_RICERCA_KM} km."
        )

    righe = [f"{carburante['emoji']} <b>{carburante['etichetta']}</b>"]
    for i, (_, r) in enumerate(df.iterrows(), start=1):
        nome = str(r.get("Nome Impianto", "")).strip() or str(r.get("Gestore", "")).strip()
        indirizzo = str(r.get("Indirizzo", "")).strip()
        comune = str(r.get("Comune", "")).strip()
        lat, lon = r["Latitudine"], r["Longitudine"]
        maps_url = f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"

        nome_e = html.escape(nome)
        bandiera_e = html.escape(str(r["Bandiera"]))
        indirizzo_e = html.escape(f"{indirizzo}, {comune}")

        righe.append(
            f"{i}. <b>{nome_e}</b> — {bandiera_e}\n"
            f"   📍 <a href=\"{maps_url}\">{indirizzo_e}</a> ({r['distanza_km']:.1f} km)\n"
            f"   💶 {r['prezzo']:.3f} €/{unita} "
            f"(prezzo effettivo stimato: {r['prezzo_effettivo']:.3f} €/{unita})"
        )
    righe.append(f"Consumo usato per il calcolo: {carburante['consumo_100km']} {unita}/100km")
    return "\n".join(righe)


def formatta_messaggio(sezioni: list[str]) -> str:
    oggi = datetime.now().strftime("%d/%m/%Y")
    testa = f"<b>Distributori consigliati - {oggi}</b>\n"
    return testa + "\n\n".join(sezioni)


def invia_messaggio_telegram(testo: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": testo,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,  # evita l'anteprima ingombrante della mappa
        },
        timeout=30,
    )
    if not resp.ok:
        print(f"Risposta Telegram: {resp.text}", file=sys.stderr)
    resp.raise_for_status()


def main():
    try:
        prezzi = scarica_csv(URL_PREZZI)
        anagrafica = scarica_csv(URL_ANAGRAFICA)

        # Uniforma i nomi delle colonne (nei due file l'id impianto ha maiuscole diverse)
        prezzi = prezzi.rename(columns={c: c.strip() for c in prezzi.columns})
        anagrafica = anagrafica.rename(columns={c: c.strip() for c in anagrafica.columns})
        prezzi = prezzi.rename(columns={"idimpianto": "idImpianto"})

        sezioni = []
        for carburante in CARBURANTI:
            df = trova_migliori_distributori(prezzi, anagrafica, carburante)
            sezioni.append(formatta_sezione(df, carburante))

        messaggio = formatta_messaggio(sezioni)
        invia_messaggio_telegram(messaggio)
        print("Messaggio inviato correttamente.")
    except Exception as e:
        print(f"Errore durante l'esecuzione: {e}", file=sys.stderr)
        try:
            invia_messaggio_telegram(f"⚠️ Errore nello script metano_bot: {e}")
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
