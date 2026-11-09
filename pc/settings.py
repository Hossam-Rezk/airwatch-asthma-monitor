# settings.py - PC side settings. Edit here; nothing in this file is shown in the dashboard.

# --------------------------------------------------------------------------- AI fallback
# True  -> warnings come from fixed, deterministic rules (no model is loaded or run).
# False -> warnings come from the trained model in model/model.joblib.
# The dashboard looks and behaves the same either way.
USE_RULE_BASED_FALLBACK = False

# --------------------------------------------------------------------------- web server
HOST = "127.0.0.1"          # "0.0.0.0" to open the dashboard from other devices on your network
PORT = 5000
OPEN_BROWSER = True         # open the dashboard in the default browser on start-up

# --------------------------------------------------------------------------- USB link to the Pico
BAUD_RATE = 115200          # USB serial ignores the baud rate, but the port still needs one
PICO_USB_VID = 0x2E8A       # Raspberry Pi USB vendor id, used to find the Pico automatically
NO_DATA_WARNING_S = 15      # connected but silent this long -> "no data" message on the dashboard
RECONNECT_EVERY_S = 2       # cable pulled out -> try to reconnect this often

# --------------------------------------------------------------------------- data
HISTORY_POINTS = 720        # readings kept in memory for the graphs (720 x 3 s = 36 min)
LOG_READINGS = True         # append every reading to data/readings_log.csv
