# Feels Like for Home Assistant

How does it **actually feel** when you step outside? **Feels Like** adds a
helper that combines air temperature, humidity, wind and **sun** into the
temperature your skin perceives. It is set up entirely from the UI.

- **Real feel (UTCI)** by default: the *Universal Thermal Climate Index*, the
  international standard for outdoor thermal comfort. It models the human body
  and adapts clothing to the weather, so it works in every climate and season.
- Separate values for **in the sun** and **in the shade**, plus a **thermal
  stress** category ("No thermal stress", "Moderate cold stress"…).
- Sun from a **solar radiation sensor**, a **light (lux) sensor**, or
  **estimated from your weather forecast's cloud cover**. No sun sensor
  required.
- Classic indices as alternatives: BoM apparent temperature, wind chill, NWS
  heat index, Humidex, and a four-season blend.
- Units are converted automatically (°F, km/h, mph, knots, Beaufort…).
- English and Norwegian (Bokmål) UI.

## Installation

### HACS (recommended)
1. HACS → ⋮ → **Custom repositories** → add
   `https://github.com/bardagi/ha-feels-like`, category **Integration**.
2. Search for **Feels Like** and download it.
3. Restart Home Assistant.

### Manual
Copy `custom_components/feels_like` into your
`config/custom_components/` folder and restart Home Assistant.

Requires Home Assistant 2025.6 or newer. The integration installs
[thermofeel](https://github.com/ecmwf/thermofeel) (ECMWF, Apache-2.0)
automatically.

## Setup

**Settings → Devices & services → Helpers → Create helper → Feels Like**,
then:

1. **Name and method**: *Real feel (UTCI)* is recommended.
2. **Sensors**: outdoor temperature, relative humidity and wind speed, plus
   where your wind sensor is mounted and where sun data comes from. *Minimum
   time between updates* (10 s – 5 min, default 1 min) limits how often the
   helper updates when input sensors report frequently: the first change is
   applied at once, later changes within the window are combined. *Precision*
   is whole degrees by default (the models are only good to about a degree);
   pick one decimal for finer steps.
3. **Sun** (if chosen): the sun sensor or weather entity, the ground surface,
   and optionally a "snow on the ground" entity.

Everything can be changed later with **Configure** on the helper.

### Entities

| Entity | When | Meaning |
|---|---|---|
| `sensor.<name>_sun` | method with sun + sun source | Feels like standing in the sun |
| `sensor.<name>_shade` | same | Feels like standing in a shadow (no direct sun) |
| `sensor.<name>` | no sun source, or a classic method | The feels-like temperature |
| `sensor.<name>_thermal_stress` | UTCI | UTCI stress category (10 levels) |

Attributes explain the calculation: method, formula used, season, solar
elevation, solar radiation and its source, direct/diffuse radiation, mean
radiant temperature, 10 m wind speed, vapour pressure, and whether inputs had
to be clamped.

If a required input is unavailable, the sensors become **unavailable** rather
than showing a wrong value. If only the sun input is unavailable, the *sun*
sensor becomes unavailable and the *shade* sensor keeps working.

## Methods

| Method | Best for | Uses sun |
|---|---|---|
| **Real feel (UTCI)** (default) | All climates and seasons | ✔ |
| **Overcast / maritime (BoM apparent temperature)** | Cloudy, windy coasts; dry heat | ✔ |
| **Four seasons** | Matches many weather apps: wind chill ≤ 10 °C, apparent temperature ≥ 14 °C, blended between | |
| **Cold and windy (wind chill)** | Cold winters, mountains (T ≤ 10 °C, wind > 4.8 km/h; air temperature otherwise) | |
| **Hot and humid (NWS heat index)** | Subtropical summers (meaningful above ~27 °C) | |
| **Hot and humid (Humidex)** | Hot, humid summers (Canadian convention) | |

Different methods give different numbers. UTCI responds more strongly to wind
than the BoM apparent temperature. For example, at 10 °C, 80 % RH and 4.5 m/s
(10 m) wind, BoM gives 6.1 °C and UTCI 1.4 °C ("slight cold stress").

### Wind sensor mounting

All formulas expect wind measured **10 m above open ground**. Wind is weaker
lower down, so it is scaled up using the logarithmic wind profile over open
terrain (roughness length 0.03 m):

| Mounting | Factor |
|---|---|
| 10 m mast | 1.00 |
| Rooftop, ~5 m | 1.14 |
| Pole, ~3 m | 1.26 |
| Garden, ~2 m | 1.38 |
| Face height, ~1.5 m (default) | 1.50 (the official JAG/TI rule) |
| Custom | your factor |

### Sun sources

| Source | How |
|---|---|
| Solar radiation sensor (W/m²) | Used directly. Most accurate. |
| Light sensor (lx) | Converted at ~120 lx per W/m² (configurable). Many cheap lux sensors saturate at 65–100 klx in full sun. |
| Weather entity | Clear-sky radiation from the sun's position (Haurwitz), reduced by the entity's `cloud_coverage` (Kasten & Czeplak). |
| None | Shade only. |

The sun's position is computed from your Home Assistant location and the
current time. This is how season and latitude enter the calculation: low
winter sun gives little warmth, high summer sun a lot. Snow reflects sunlight
strongly, so a sunny winter day on snow feels noticeably warmer.

## How it is calculated

1. Wind is converted to the 10 m equivalent.
2. Measured (or estimated) global radiation is split into direct and diffuse
   parts (Erbs et al. 1982).
3. The **mean radiant temperature** is computed for a standing person
   (Di Napoli et al. 2020, the ECMWF method). It includes diffuse sky
   radiation, radiation reflected from the ground and, in the sun, the direct
   beam weighted by the body's projected area. Long-wave radiation from the
   surroundings is assumed to be at air temperature, so Tmrt = air temperature
   when there is no sun.
4. **UTCI** is evaluated with the operational polynomial of Bröde et al.
   (2012) via thermofeel. Inputs are clamped to the polynomial's valid range
   (−50…50 °C, wind 0.5–17 m/s, Tmrt − Ta −30…70 °C).

Classic formulas:

- **BoM apparent temperature** (shade): `Ta + 0.33·e − 0.70·ws − 4.00`, with
  `e = RH/100 · 6.105 · exp(17.27·Ta/(237.7+Ta))`. For the sun value, the extra
  direct-beam radiation absorbed by the body (ΔQ) is added with the BoM
  radiation term `0.70·ΔQ/(ws + 10)`.
- **Wind chill** (JAG/TI, 2001): `13.12 + 0.6215·T − 11.37·v^0.16 + 0.3965·T·v^0.16`
  (v in km/h at 10 m).
- **Heat index**: NOAA/WPC algorithm (Rothfusz regression with the simple
  formula below 80 °F and the low/high-humidity adjustments).
- **Humidex**: `T + 0.5555·(e − 10)`.

### Limitations

- UTCI's polynomial approximation has a typical error of about 1 °C compared
  with the full physiological model.
- Cold clear-sky nights (radiative cooling) are not modelled.
- The Erbs split is fitted on hourly data and may overestimate diffuse
  radiation on clear days.
- The Kasten & Czeplak cloud model was fitted on Hamburg data; estimates are
  rough under partial cloud.
- "Shade" means a shadow in otherwise open surroundings (full sky view).

## Sources

- Bröde P. et al. (2012), *Deriving the operational procedure for the Universal
  Thermal Climate Index (UTCI)*, Int J Biometeorol 56. ·
  [thermofeel](https://thermofeel.readthedocs.io/) ·
  [pythermalcomfort](https://pythermalcomfort.readthedocs.io/) (cross-check)
- Di Napoli C. et al. (2020), *Mean radiant temperature from global-scale
  numerical weather prediction models*, Int J Biometeorol 64 ·
  [PMC7295834](https://pmc.ncbi.nlm.nih.gov/articles/PMC7295834/)
- Australian Bureau of Meteorology apparent temperature (Steadman 1994) ·
  [Wikipedia](https://en.wikipedia.org/wiki/Apparent_temperature)
- [Wind chill (JAG/TI)](https://en.wikipedia.org/wiki/Wind_chill) ·
  [MetPy windchill (face-level ×1.5)](https://unidata.github.io/MetPy/latest/api/generated/metpy.calc.windchill.html)
- [NOAA WPC heat index equation](https://www.wpc.ncep.noaa.gov/html/heatindex_equation.shtml)
- [Humidex](https://en.wikipedia.org/wiki/Humidex)
- Erbs, Klein & Duffie (1982), Solar Energy 28 ·
  [pvlib](https://pvlib-python.readthedocs.io/)
- Haurwitz (1945); Kasten & Czeplak (1980), Solar Energy 24
- [Roughness lengths (Davenport/WMO)](https://wind-stats.readthedocs.io/en/latest/user_guide/roughness_length.html)

## Development

Tooling is managed with [uv](https://docs.astral.sh/uv/) (dependencies are locked in
`uv.lock`) and [prek](https://github.com/j178/prek) for git hooks.

```bash
uv sync                  # create .venv with the locked dev dependencies
uv run prek install      # once per clone: run the checks on every commit
uv run prek run --all-files   # ruff, ruff format, ty, typos, workflow lint, translations
uv run pytest            # tests + coverage (fails below the threshold in pyproject.toml)
```

`pytest` needs Linux, macOS or WSL: Home Assistant does not run natively on Windows
(it imports `fcntl`). Linting and type checking (`prek`, `ruff`, `ty`) work everywhere.
On WSL, keep the virtualenv off the Windows mount for speed, e.g.
`UV_PROJECT_ENVIRONMENT=~/.venvs/feels_like uv run pytest`.

### Translations

Do not edit `custom_components/feels_like/translations/*.json` directly. Edit
`translations_src/<lang>.json` and run `python scripts/generate_translations.py`.
Repeated strings are written once and reused with `"[%key:path::to::string%]"`
(Home Assistant does not resolve these for custom integrations, so the generator
expands them). `pytest` fails if the generated files are out of date or a source repeats
a string.

The `prek` hook regenerates the translations when `translations_src/` changes. It
modifies files, so the commit stops once; stage the regenerated files and commit again.

## License

MIT
