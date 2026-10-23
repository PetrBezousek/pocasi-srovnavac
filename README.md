# Srovnávač předpovědí počasí

Automaticky sbírá předpovědi od více poskytovatelů počasí a porovnává je se
skutečně naměřenými hodnotami ze stanice ČHMÚ. Po pár týdnech ukáže, které API
předpovídá pro dané místo nejpřesněji.

Výchozí nastavení: **Ústí nad Orlicí**, profesionální stanice ČHMÚ 11679.

## Co se testuje

Bez registrace (fungují hned):

| Poskytovatel | Co to je |
|---|---|
| Open-Meteo | 11 modelů zvlášť: ICON-D2, ICON-EU, ECMWF IFS, ECMWF AIFS, GFS, Météo-France, UK Met Office, KNMI, DMI, GEM a automatický výběr |
| MET Norway (Yr) | Norský meteorologický ústav |
| Bright Sky | Model MOSMIX německé DWD, přímo pro stanici Ústí nad Orlicí |

S API klíčem (volitelné, zdarma po registraci):

| Poskytovatel | Registrace | Název tajemství v GitHubu |
|---|---|---|
| OpenWeatherMap | openweathermap.org/api | `OPENWEATHERMAP_API_KEY` |
| WeatherAPI.com | weatherapi.com | `WEATHERAPI_API_KEY` |
| Visual Crossing | visualcrossing.com | `VISUALCROSSING_API_KEY` |

Hodnotí se teplota, relativní vlhkost, rychlost větru a srážky, zvlášť pro
předpovědi na 1–24 h, 25–48 h a 49–72 h dopředu.

## Zprovoznění (cca 10 minut)

1. Na GitHubu založte nový repozitář (může být veřejný i soukromý) a nahrajte
   do něj obsah této složky. Nejjednodušší je přes webové rozhraní:
   **Add file → Upload files** a přetáhnout všechny soubory a složky.
   Pozor, aby se nahrála i skrytá složka `.github`.
2. **Settings → Actions → General → Workflow permissions** nastavte na
   *Read and write permissions* a uložte.
3. **Actions → Sběr předpovědí → Run workflow** spustí první sběr hned.
   Dál poběží samo každé 3 hodiny.
4. Dashboard: **Settings → Pages → Source: Deploy from a branch**,
   větev `main`, složka `/docs`. Za minutu bude na adrese
   `https://<uživatel>.github.io/<repozitář>/`.
   (U soukromého repozitáře vyžadují Pages placený účet GitHubu; jinak
   repozitář nastavte jako veřejný.)
5. Volitelně přidejte API klíče: **Settings → Secrets and variables →
   Actions → New repository secret** s názvy z tabulky výše.

## Kdy budou výsledky

- **Hned:** graf „Co hlásí teď“ a dostupnost API.
- **Po 1–2 dnech:** první čísla pro předpověď na 24 h.
- **Po 2–4 týdnech:** spolehlivý žebříček. Ideálně nechat běžet aspoň měsíc,
  aby se vystřídalo víc typů počasí.

## Jiné místo

V `config.py` změňte souřadnice a stanici ČHMÚ. Identifikátor stanice (WSI)
najdete v souboru `meta1-*.json` na
https://opendata.chmi.cz/meteorology/climate/now/metadata/ (sloupce WSI, název,
délka, šířka). Vybírejte stanici s 10minutovými daty a souřadnice předpovědí
nastavte na polohu stanice.

## Soubory

| Soubor | Účel |
|---|---|
| `collect.py` | stáhne předpovědi a měření, uloží do `data/` |
| `evaluate.py` | spočítá chyby, zapíše `docs/results.json` |
| `docs/index.html` | dashboard |
| `data/forecasts/*.csv` | všechny stažené předpovědi |
| `data/observations.csv` | hodinová měření ČHMÚ |
| `data/runs.csv` | log volání API (úspěšnost, odezva) |
| `tests/` | testy parserů a vyhodnocení (`python -m unittest`) |

Jen standardní Python 3.10+, nic se neinstaluje. Lokálně:
`python collect.py && python evaluate.py`, pak `python -m http.server -d docs`.

## Poznámky k metodice

- Srážky se ukládají jako úhrn končící v daném čase. U WeatherAPI a Visual
  Crossing se předpokládá úhrn za hodinu začínající časem položky,
  u OpenWeatherMap úhrn za předchozí 3 hodiny (dle dokumentace). Proto se
  srážky hodnotí v 6hodinových oknech, kde posun o hodinu téměř nevadí.
- OpenWeatherMap má zdarma jen 3hodinový krok, hodnotí se tedy na menším vzorku.
- Visual Crossing: 8 stažení denně × 96 hodin = 768 záznamů, pod limitem 1000.
- Soubory ČHMÚ v adresáři `now` se mažou po 24 h. Když sběr víc než den
  neběží, měření za tu dobu chybí (předpovědi pro ty hodiny se nevyhodnotí).
- Data ČHMÚ: zdroj Český hydrometeorologický ústav. Open-Meteo: CC BY 4.0,
  zdarma pro nekomerční použití.
