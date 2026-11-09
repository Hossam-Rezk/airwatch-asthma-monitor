# AirWatch: AI Asthma-Risk Monitor

A Raspberry Pi Pico reads **temperature, humidity, air quality and dust**. It sends the readings to
your computer over a **USB cable**. A local web dashboard shows them as four live graphs, and an AI
model warns when the air could trigger asthma. The Pico's RGB LED and buzzer show the same risk level.

```
 Pico + sensors  ──USB cable──►  PC: app.py  ──►  browser dashboard (http://127.0.0.1:5000)
   (MicroPython)  ◄─risk level──   (AI model)
```

**Just want to see it?** You don't need any hardware. Do [Step 6](#step-6--get-the-code-and-start-the-dashboard),
then run `start.bat --demo` (Windows) or `./start.sh --demo` (Linux/macOS).

---

## Contents

1. [Parts list](#step-1--parts-list)
2. [Wire the hardware](#step-2--wire-the-hardware)
3. [Install MicroPython on the Pico](#step-3--install-micropython-on-the-pico)
4. [Copy the firmware to the Pico](#step-4--copy-the-firmware-to-the-pico)
5. [Install Python on the computer](#step-5--install-python-on-the-computer)
6. [Get the code and start the dashboard](#step-6--get-the-code-and-start-the-dashboard)
7. [Connect the Pico and use the dashboard](#step-7--connect-the-pico-and-use-the-dashboard)
8. [Calibrate the sensors (recommended)](#step-8--calibrate-the-sensors-recommended)
- [Configuration](#configuration) · [AI fallback switch](#the-ai-fallback-switch) ·
  [The AI model](#the-ai-model) · [Troubleshooting](#troubleshooting) · [Project layout](#project-layout)

---

## Step 1 – Parts list

| Qty | Part | Used for |
|---|---|---|
| 1 | Raspberry Pi Pico, Pico W, Pico 2 or Pico 2 W | The controller |
| 1 | USB data cable (micro-USB for Pico / Pico W, also fine for Pico 2) | Power + data to the PC. **Charge-only cables do not work.** |
| 1 | DHT-22 (AM2302), 3-pin module | Temperature and humidity |
| 1 | MQ-135 gas sensor module (with AO pin) | Air quality |
| 1 | Sharp GP2Y1010AU0F dust sensor (6-wire cable) | Dust (PM2.5 equivalent) |
| 1 | 150 Ω resistor + 220 µF electrolytic capacitor | Dust-sensor LED supply (datasheet circuit) |
| 10 | 5.6 kΩ resistors | Two voltage dividers (5 V sensor output → 3 V for the Pico) |
| 1 | RGB LED, common cathode, + 3 × 220 Ω resistors | Risk colour |
| 1 | Active buzzer (5 V) + 2N3904 NPN transistor + 880 Ω resistor (or 820 Ω / 1 kΩ) | Risk sound |
| – | Breadboard and jumper wires | |

---

## Step 2 – Wire the hardware

> Unplug the USB cable while you wire. Pin numbers below are **physical pin numbers** (1–40)
> printed on the Pico's underside, with the matching **GPIO name** (GP…). Pin 1 is at the top left
> when the USB socket points up.

### 2.1 Power rails

Use the breadboard's two rails:

| Rail | Connect to Pico | Notes |
|---|---|---|
| **5 V rail** | Pin 40 **VBUS** | 5 V straight from the USB cable. Feeds the MQ-135, the dust sensor and the buzzer. |
| **3.3 V rail** | Pin 36 **3V3(OUT)** | Feeds the DHT-22 only |
| **GND rail** | Pin 38 **GND** (any GND pin works: 3, 8, 13, 18, 23, 28, 33, 38) | **Every** part's ground goes here |

### 2.2 Voltage dividers (build two)

The MQ-135 and the dust sensor output up to 5 V, but the Pico's analog pins only take 3.3 V. Each
analog output goes through a divider made from five 5.6 kΩ resistors:

```
 sensor output ──[5.6k]──[5.6k]──┬──[5.6k]──[5.6k]──[5.6k]── GND
                    R1 = 11.2 kΩ  │        R2 = 16.8 kΩ
                                  └──► Pico analog pin
```

5 V in gives 3.0 V at the Pico (ratio 0.60, `ADC_DIVIDER_RATIO` in `pico/config.py`).

### 2.3 DHT-22 (temperature and humidity)

| DHT-22 module pin | Connect to |
|---|---|
| `+` / VCC | 3.3 V rail |
| `out` / DATA | Pin 20 **GP15** |
| `−` / GND | GND rail |

A 3-pin module already has its pull-up resistor. With a bare 4-pin sensor, add 10 kΩ between DATA and
3.3 V and leave pin 3 unconnected.

### 2.4 MQ-135 (air quality)

| MQ-135 module pin | Connect to |
|---|---|
| VCC | 5 V rail |
| GND | GND rail |
| AO | **Divider A** input. Its middle point goes to pin 32 **GP27 (ADC1)** |
| DO | Not used |

The MQ-135's heater gets warm; that is normal. Readings are ignored for the first 30 s after
power-up.

### 2.5 Sharp GP2Y1010AU0F (dust)

Pins are numbered 1–6 along the sensor's connector. Wire colours differ between cables, so go by the
number printed next to the connector or in the datasheet.

| Sensor pin | Name | Connect to |
|---|---|---|
| 1 | V-LED | 5 V rail **through the 150 Ω resistor**. Also put the **220 µF capacitor** from pin 1 to GND (`+` leg on pin 1). |
| 2 | LED-GND | GND rail |
| 3 | LED | Pin 21 **GP16** (the firmware pulses it; LOW = IR LED on) |
| 4 | S-GND | GND rail |
| 5 | Vo | **Divider B** input. Its middle point goes to pin 31 **GP26 (ADC0)** |
| 6 | Vcc | 5 V rail |

### 2.6 RGB LED (common cathode)

| LED leg | Connect to |
|---|---|
| Red | 220 Ω → pin 14 **GP10** |
| Common (the longest leg) | GND rail |
| Green | 220 Ω → pin 15 **GP11** |
| Blue | 220 Ω → pin 16 **GP12** |

With a common-anode LED, connect the long leg to 3.3 V instead and set `LED_COMMON_ANODE = True` in
`pico/config.py`.

### 2.7 Buzzer (through a transistor)

```
 Pin 19 GP14 ──[880 Ω]── 2N3904 base
                         2N3904 emitter ── GND rail
                         2N3904 collector ── buzzer (−)
                         buzzer (+) ── 5 V rail
```

Hold the 2N3904 with its flat face towards you and legs down: the legs are **E, B, C** from left to
right.

### 2.8 Complete pin map

| Pico pin | GPIO | Goes to |
|---|---|---|
| 14 | GP10 | RGB red (220 Ω) |
| 15 | GP11 | RGB green (220 Ω) |
| 16 | GP12 | RGB blue (220 Ω) |
| 19 | GP14 | Buzzer transistor base (880 Ω) |
| 20 | GP15 | DHT-22 data |
| 21 | GP16 | Dust sensor pin 3 (LED) |
| 31 | GP26 / ADC0 | Divider B middle (dust sensor Vo) |
| 32 | GP27 / ADC1 | Divider A middle (MQ-135 AO) |
| 36 | 3V3(OUT) | 3.3 V rail (DHT-22) |
| 38 | GND | GND rail |
| 40 | VBUS | 5 V rail (MQ-135, dust sensor, buzzer) |

To use other pins, change the `PIN_…` values in `pico/config.py`.

### 2.9 Check before powering up

- [ ] No 5 V wire touches a GP pin directly. Both analog outputs go through their dividers.
- [ ] All grounds are on the same GND rail.
- [ ] The 220 µF capacitor's `+` leg is on the dust sensor's pin 1 side.
- [ ] The DHT-22 is on 3.3 V, not 5 V.

---

## Step 3 – Install MicroPython on the Pico

1. Download the MicroPython firmware (`.uf2`) for your exact board from
   <https://micropython.org/download/?vendor=Raspberry%20Pi>: *Pico*, *Pico W*, *Pico 2* or *Pico 2 W*.
2. Hold down the **BOOTSEL** button on the Pico and plug in the USB cable. Then release the button.
3. A USB drive called **RPI-RP2** (or **RP2350**) appears. Drag the `.uf2` file onto it.
4. The drive disappears and the Pico restarts with MicroPython. This only has to be done once.

---

## Step 4 – Copy the firmware to the Pico

1. Install **Thonny** from <https://thonny.org>.
2. Open Thonny. In the bottom-right corner, click the interpreter name and choose
   **MicroPython (Raspberry Pi Pico)**. The Shell shows `>>>`.
3. In Thonny, open each of the four files in this project's **`pico/`** folder:
   `main.py`, `config.py`, `sensors.py`, `alerts.py`.
4. For each file, use **File → Save as… → Raspberry Pi Pico**, and keep exactly the same file name.
5. Optional test: open `main.py` from the Pico and press the green **Run** button. A line like
   `{"type": "reading", "temperature_c": 24.8, …}` should appear every 3 s. Press **Stop**.
6. **Close Thonny.** It keeps the USB port busy while it is open, so the dashboard could not connect.
7. Unplug the Pico and plug it back in. The LED blinks blue while it starts. `main.py` now runs by
   itself whenever the Pico has power.

Whenever you edit `pico/config.py`, save it to the Pico again the same way.

---

## Step 5 – Install Python on the computer

- **Windows:** install Python 3.9 or newer from <https://www.python.org/downloads/>.
  **Tick "Add python.exe to PATH"** on the first installer screen.
- **macOS:** install it from python.org, or run `brew install python`.
- **Linux:** usually already installed. Also run the command below once, then log out and back in, so
  your user may open USB serial ports:

```bash
sudo usermod -a -G dialout $USER
```

---

## Step 6 – Get the code and start the dashboard

**1. Get the code**, either with Git:

```bash
git clone https://github.com/Hossam-Rezk/airwatch-asthma-monitor.git
```

or by downloading the ZIP from the GitHub page (**Code → Download ZIP**) and unzipping it.

**2. Start the dashboard:**

- **Windows:** open the folder and double-click **`start.bat`**.
- **Linux / macOS:** open a terminal in the folder and run:

```bash
./start.sh
```

The first start creates a private Python environment (`.venv`) and installs the packages: `flask`,
`pyserial`, `scikit-learn` and `waitress`. This takes 1–2 minutes and needs internet. Later starts
take a few seconds and work offline.

When you see `Dashboard running at http://127.0.0.1:5000`, your browser opens the dashboard by
itself. If it doesn't, open that address manually. Keep the start window open while you use the
dashboard. To stop it, close the window or press `Ctrl+C`.

---

## Step 7 – Connect the Pico and use the dashboard

1. Plug the Pico into the computer (Thonny must be closed).
2. On the **Dashboard**, the Pico is pre-selected in the **USB port** list and marked with ✓. If it is
   missing, press the refresh button next to the list.
3. Press **Connect**. The badge turns green (**Connected**) and the station name appears.
4. Within a few seconds the four graphs start moving and the **AI Analysis & Warnings** panel shows
   the risk level. The Pico's LED and buzzer follow the same level.
5. Press **Disconnect** when you are done. The Pico then switches back to its own built-in thresholds:
   it shows the local risk colour with a short blue flash.

| Dashboard area | What it shows |
|---|---|
| Navigation bar | Dashboard · Readings (table + **Download CSV**) · Help · live connection indicator |
| Raspberry Pi Pico panel | Port list, Connect/Disconnect, connection state, station, time since the last reading |
| AI Analysis & Warnings | **SAFE / WARNING / DANGER**, the main causes, what to do, and sensor problems |
| Four graphs | Temperature (°C), Humidity (%), Air Quality (AQI 0–500), Dust (µg/m³, PM2.5 equivalent). Dashed lines mark the warning (amber) and danger (red) limits. Hover a graph for exact values. |

Connection states: **Connected** (green), **Connecting / Reconnecting** (amber),
**No data** (connected, but nothing has arrived for 15 s) and **Disconnected**. If the cable comes
out, the dashboard keeps trying to reconnect until you press Disconnect.

Every reading is also saved to `pc/data/readings_log.csv`.

**Air quality is shown as an AQI.** The MQ-135 reacts to a mix of gases (CO, NO₂, NH₃, benzene,
smoke). Without lab calibration it cannot report a reliable ppm for any one gas. So the firmware turns
its output into a **0–500 index on the US EPA AQI scale**: 0–50 good, 51–100 moderate, 101–150
unhealthy for sensitive groups, 151–200 unhealthy, 201–300 very unhealthy, 301+ hazardous.

---

## Step 8 – Calibrate the sensors (recommended)

The default calibration values in `pico/config.py` are only starting guesses. Measure your own
sensors in clean air:

1. Run the Pico on a window sill or in a ventilated room for at least 10 minutes. Leave the MQ-135
   powered for an hour the first time.
2. Open Thonny (disconnect the dashboard first) and press **Stop** to get the `>>>` prompt.
3. In the Shell, type:

```python
from sensors import MQ135Sensor, SharpDustSensor
import asyncio
print("GAS_CLEAN_V =", MQ135Sensor().measure_clean_air_volts())
print("dust volts  =", asyncio.run(SharpDustSensor().measure())["dust_v"])
```

4. In `pico/config.py`, set `GAS_CLEAN_V` to the first number and `DUST_BASELINE_V` to the second.
5. Save `config.py` to the Pico, close Thonny and replug the Pico.

---

## Configuration

**PC side: `pc/settings.py`.** None of these settings appear in the dashboard.

| Setting | Default | Meaning |
|---|---|---|
| `USE_RULE_BASED_FALLBACK` | `False` | **AI fallback switch** (see below) |
| `HOST` / `PORT` | `127.0.0.1` / `5000` | Dashboard address. Use `HOST = "0.0.0.0"` to open it from other devices on your network. |
| `OPEN_BROWSER` | `True` | Open the browser on start |
| `NO_DATA_WARNING_S` | `15` | Seconds without readings before "No data" is shown |
| `HISTORY_POINTS` | `720` | Readings kept in memory |
| `LOG_READINGS` | `True` | Write `pc/data/readings_log.csv` |

Start options, which work the same with `start.bat`, `./start.sh` or `python pc/app.py`:

| Option | Effect |
|---|---|
| `--demo` | Adds **"Demo Pico (simulated readings)"** to the port list |
| `--port 8080` | Uses another web port |
| `--no-browser` | Does not open the browser |

**Pico side: `pico/config.py`.** Station name, pins, sample interval (3 s), calibration, LED and
buzzer patterns, and the stand-alone thresholds.

### The AI fallback switch

`USE_RULE_BASED_FALLBACK` in `pc/settings.py`. Restart the dashboard after changing it.

- **`False` (default): ML mode.** Every reading goes to the trained Random Forest. The predicted class
  is the risk level. To name the causes, the model is asked again with each reading replaced by a
  clean-air value. The readings that lower the predicted risk most are listed as causes.
- **`True`: rule mode.** Fixed, deterministic thresholds: dust 35.5 / 55.5 µg/m³ (EPA PM2.5), air
  quality AQI 101 / 201, temperature 32 / 38 °C (cold at 10 °C or below), humidity 80 / 90 % (dry at
  25 % or below). High dust and high gas together count as DANGER. No model is loaded.

Both modes return the same fields and wording, so the dashboard looks identical. The active mode is
written only to the console log. In ML mode the app also falls back to the rules by itself if the
model cannot be loaded or retrained, so warnings never stop.

---

## The AI model

Full report: [`pc/model/model_report.md`](pc/model/model_report.md), regenerated on every training run.

**Dataset.** The *Air Quality and Pollution Assessment* dataset by Mujtaba Mateen (Kaggle,
<https://www.kaggle.com/datasets/mujtabamatin/air-quality-and-pollution-assessment>, Apache 2.0). It
was found and downloaded through the Kaggle MCP server and is saved as
`pc/data/updated_pollution_dataset.csv`. It has 5,000 rows of temperature, humidity, PM2.5, PM10, NO₂,
SO₂, CO, distance to industry and population density, labelled Good / Moderate / Poor / Hazardous.

**Preprocessing**
1. Kept only what the Pico can measure: temperature, humidity, PM2.5 (the dust sensor), and CO + NO₂
   (the gas sensor).
2. Clipped humidity to 0–100 % (195 rows were above 100 %).
3. Built a gas index on the MQ-135's 0–500 scale. CO and NO₂ are converted to EPA AQI sub-indices and
   added. The sum is scaled linearly so the training data's 1st percentile is 0 and its 99th
   percentile is 300.
4. Mapped the labels: Good → SAFE, Moderate → WARNING, Poor and Hazardous → DANGER.
5. Applied an EPA PM2.5 health floor: PM2.5 ≥ 35.5 is at least WARNING and ≥ 55.5 is DANGER
   (144 labels raised).
6. Made a stratified 80/20 train/test split. In the training split only:
   - added 600 rows with only the dust value raised, so the model learns that dust alone can be
     dangerous;
   - blanked 3 % of values at random, so it copes with a failed sensor.

**Model selection.** Three lightweight models were compared with 5-fold cross-validation on the
training split, and the best CV macro-F1 was chosen. The test columns are for comparison only.

| Model | CV macro-F1 | CV accuracy | Test accuracy | Test macro-F1 |
|---|---|---|---|---|
| Logistic regression | 0.862 | 0.867 | 0.877 | 0.872 |
| Decision tree (depth 6) | 0.890 | 0.894 | 0.898 | 0.894 |
| **Random forest (150 trees)** | **0.907** | **0.911** | **0.910** | **0.906** |

**Selected model.** Median imputer → Random Forest (150 trees, max depth 12, balanced class weights).
Its inputs are `temperature_c, humidity_pct, dust_ugm3, aqi_proxy`. The file is about 5 MB and
answers in milliseconds.

**Results on the held-out test set (1,000 rows):** accuracy 0.910, balanced accuracy 0.905,
macro-F1 0.906, DANGER recall 0.894, and **0 % of DANGER rows predicted SAFE**. Per class
(precision / recall): SAFE 0.94 / 0.95, WARNING 0.83 / 0.87, DANGER 0.96 / 0.89. If one sensor fails,
macro-F1 stays between 0.87 and 0.90, except without the gas sensor, where it drops to 0.69.

**Retraining.** The trained model is included. It retrains itself automatically, in a few seconds, if
it is missing or was saved with a different scikit-learn version. To retrain by hand on Windows:

```bash
.venv\Scripts\python pc\train_model.py
```

On Linux or macOS:

```bash
.venv/bin/python pc/train_model.py
```

**Limitations.** The labels describe general air quality, not asthma attacks. The data is regional
outdoor data, while the Pico measures one spot with low-cost sensors. Treat the output as a
**screening aid, not a medical device**.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `start.bat` says Python is missing | Install Python with "Add python.exe to PATH" ticked (Step 5), then run it again |
| No Pico in the port list | Use a data cable and try another USB port. Check that MicroPython is installed (Step 3). Press the refresh button. |
| "…is in use by another program" | Close Thonny or any serial monitor, then press Connect again |
| Connected, but "No data" | The Pico files are missing or have an error. Open the Pico in Thonny to see the message, fix it, close Thonny and replug the Pico. |
| Air quality empty for the first 30 s | Normal: the MQ-135 is warming up |
| "Temperature/humidity sensor is not giving valid readings" | Check the DHT-22 data wire is on GP15 and the sensor is on 3.3 V |
| Dust always 0 or always high | Check pin 3 is on GP16 and the 150 Ω resistor and 220 µF capacitor are in place, then calibrate (Step 8) |
| Air quality always 0 or always 500 | Check the divider on GP27, then calibrate `GAS_CLEAN_V` (Step 8) |
| The Pico keeps rebooting in Thonny | `WATCHDOG_ENABLED` is `True` in `pico/config.py`; set it to `False` while developing |
| Linux: permission denied on `/dev/ttyACM0` | Run `sudo usermod -a -G dialout $USER`, then log out and back in |
| Port 5000 is busy | Start with `--port 8080` |

---

## Project layout

```
├── start.bat / start.sh       one-click setup and start
├── requirements.txt
├── pc/                        runs on the computer
│   ├── app.py                 web server, API and wiring of the parts below
│   ├── pico_link.py           USB serial connection, auto-reconnect, demo device
│   ├── analysis.py            ML engine + rule engine (same output format)
│   ├── settings.py            settings, including USE_RULE_BASED_FALLBACK
│   ├── train_model.py         preprocessing, model comparison, evaluation, report
│   ├── data/                  Kaggle dataset (+ readings_log.csv at run time)
│   ├── model/                 model.joblib, metrics.json, model_report.md
│   └── static/                dashboard (HTML, CSS, JS; graphs need no external library)
├── pico/                      copy these four files onto the Pico
│   ├── main.py                USB protocol, sampling loop, stand-alone fallback
│   ├── config.py              pins, calibration, intervals, thresholds
│   ├── sensors.py             DHT-22, MQ-135, Sharp dust sensor drivers
│   └── alerts.py              RGB LED and buzzer patterns
└── legacy/                    the earlier Wi-Fi/HTTP server version, kept for reference
```

### USB protocol

The Pico and the PC exchange one JSON object per line, in both directions:

```
Pico → PC  {"type":"hello","station_id":"METRO-ST-01","fw":"1.0.0","interval_s":3}
Pico → PC  {"type":"reading","seq":42,"temperature_c":26.1,"humidity_pct":48.0,"dust_ugm3":9.5,
            "aqi_proxy":38,"sensor_ok":{"dht":true,"mq135":true,"dust":true},"mq135_warming":false,...}
PC → Pico  {"cmd":"hello"}
PC → Pico  {"cmd":"risk","risk":"WARNING"}
```

Lines that are not JSON, such as the MicroPython banner or debug prints, are ignored. If the Pico gets
no risk level for 12 s, it switches to its own thresholds.

---

## Credits and licence

- Dataset: *Air Quality and Pollution Assessment* by Mujtaba Mateen, Kaggle, Apache 2.0.
- Not a medical device. Always follow your doctor's asthma action plan.
