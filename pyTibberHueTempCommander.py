#!/usr/bin/python3
#
# Run:
#     ./pyTibberHueTempCommander.py &
#
# Output:
#     Standard output: Only one-time information when starting the program
#     Syslog: All other outputs (in order for disown to work)
#
# Tested 2023-08-05 on:
#     Debian GNU/Linux 11 (bullseye), 5.10.0-23-amd64 x86_64
#     Python 3.9.2
#     Tibber v1-beta Graph QL API
#     Philips HUE "ZLLTemperature" sensor (inside motion sensor device), swversion 2.53.6 via Bridge API
#     Philips HUE "On/Off plug-in unit" smart plug, swversion "1.104.2" via Bridge API
#
# apt-get install python3-pip
# python3 -m pip install requests
# python3 -m pip install phue
# python3 -m pip install numpy
#
# No timezone support (assumes local time on runtime environment is the same as response from Tibber's API)
# Known bug: If Daylight Savings and Tibber responds with either 23 or 25 prices, prices can't be used correctly that day

import inspect
import json
import numpy
import requests
import syslog
import time
from datetime import datetime
from phue import Bridge



# Global variables for your infrastructure and price aggressiveness apetite
tibberAuthToken = "generateYourTokenInPhilipsHueBridgeAndPasteHere" # E.g.: "njfzEM5Fh1XrzrhI2cbwfuLgmB0fQndjoDY02jLtWSJ"
philipsHueBridgeAddress = "192.168.0.2" # E.g.: "192.168.13.37"
philipsHuePlugName = "Garage plug 1" # E.g.: "Garage plug 1"
philipsHueSensorName = "Hue temperature sensor 2" # E.g.: "Hue temperature sensor 1" (Each motion sensor paired with Bridge gets sequence numbered sensor name. If you rename in HUE app, only the motion sensor sensor gets renamed. The temperature sensor remains sequence numbere name like this in the same sequence number as the motion sensor.)
minTemperatureThreshold = 2 # Degrees Celcius (e.g. 2)
maxTemperatureThreshold = 10 # Degrees Celcius (e.g. 10)
normalMinTemperatureThreshold = 4 # Degrees Celcius (e.g. 4)
normalMaxTemperatureThreshold = 7 # Degrees Celcius (e.g. 7)
lowEnergyPricePercentileThreshold = 15 # Which percentile to switch from normal thermostat range to the min/max extremes when price is cheap (e.g. 15)
highEnergyPricePercentileThreshold = 70 # Which percentile to switch from normal thermostat range to the min/max extremes when price is expensive (e.g. 70)
radiatorPower = 1250 # Power in W for how much your radiator in the smart plug generates (e.g. 1250 for 1,25kW)


class PowerSession:
    """
    Object with variables needed to estimate Tibber cost for a session with power on
    """
    def __init__(self, powerOnTime, powerOffTime, powerOnPrice, powerOffPrice, startTemp, endTemp):
        self.powerOnTime = powerOnTime
        self.powerOffTime = powerOffTime
        self.powerOnPrice = powerOnPrice
        self.powerOffPrice = powerOffPrice
        self.startTemp = startTemp
        self.endTemp = endTemp

def estimateCost(seconds, price):
    """
    Input: Time (s), Price (SEK per kWh)
    Output: Cost (SEK)
    """
    return seconds / 3600 * radiatorPower / 1000 * price

def syslogPrice(sessionCost, startTemp, endTemp, duration):
    """
    Prints rounded price to syslog
    Input: float (session cost), start temp (float), end temp (float), duration (String)
    No output
    """
    syslog.syslog(syslog.LOG_INFO, f"PRICE: Estimated Tibber cost for last power on session is [{round(sessionCost, 2)}] SEK ([{round(startTemp, 1)}] --> [{round(endTemp, 1)}] | [{duration}])")

def getTodayAndTomorrowEnergyPrices():
    """
    Get energy prices from Tibber's API. 
    Either returns exception or the JSON response in Python dictionary format. 
    Warning: If Tibber changes their response, this change will be forwarded without any validation in the JSON response
    """
    tibberUrl = "https://api.tibber.com/v1-beta/gql"

    headers = {
       "Authorization": f"Bearer {tibberAuthToken}",
       "Content-Type": "application/json",
    }

    query = {
        "query": "{viewer {homes {currentSubscription {priceInfo {today {total startsAt} tomorrow {total startsAt}}}}}}"
    }

    try:
        response = requests.get(tibberUrl, headers=headers, params=query)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.HTTPError as e:
        raise Exception(f"Error fetching data from Tibber (HTTP Error): {e}")
    except requests.exceptions.RequestException as e:
        raise Exception(f"Error fetching data from Tibber (Catch all): {e}")

def getTemperature(device):
    """
    Get temperature from Philips HUE's API
    Input: String name of the sensor to get temperature from (case sensitive)
    Output: Temperature in degrees Celcius, precision = 2 decimals (current limitation from hardware/API)
    """
    try:
        hueBridge = Bridge(philipsHueBridgeAddress)
        #hueBridge.connect() # this is only needed the first time connecting to the bridge to pair the software with the bridge (click button on bridge, run this connect within 30s by uncommenting the line, then comment it back in (username is stored in ~/.python_hue))
        sensor = hueBridge.get_sensor(int(hueBridge.get_sensor_id_by_name(device)))
        
        if isinstance(sensor["state"]["temperature"], int):
            return sensor["state"]["temperature"] / 100
        
        return None

    except ConnectionError as e:
        syslog.syslog(syslog.LOG_ERR, f"ERROR: Failed connecting to the Philips Hue bridge: {e}")
        return None
    except Exception as e:
        syslog.syslog(syslog.LOG_ERR, f"ERROR: An unexpected error occurred connecting to the Philips Hue bridge: {e}")
        return None

def ensurePowerState(device, state, thisPowerSession, currentEnergyPrice, currentTemperature):
    """
    Ensures provided power state on the provided Philips HUE smart plug (that is accessible via the lamp functions). 
    Input: String (name (case sensitive) of HUE sensor, Boolean (True = set to on, False = set to off), PowerSession (object of class), Double (current energy price)
    No output
    """
    if isinstance(device, str) and isinstance(state, bool) and isinstance(thisPowerSession, PowerSession) and isinstance(currentEnergyPrice, (int, float))\
        and isinstance(currentTemperature, (int, float)):
        try:
            hueBridge = Bridge(philipsHueBridgeAddress)
            currentPowerState = hueBridge.get_light(int(hueBridge.get_light_id_by_name(device)), "on")
            if currentPowerState != state:
                hueBridge.set_light(int(hueBridge.get_light_id_by_name(device)), "on", state)
                syslog.syslog(syslog.LOG_INFO, f"ACTION: Device = [{device}] - Changed power state to [{state}]")
                if state == True:
                    thisPowerSession.powerOnTime = datetime.now()
                    thisPowerSession.powerOnPrice = currentEnergyPrice
                    thisPowerSession.startTemp = currentTemperature
                else:
                    thisPowerSession.powerOffTime = datetime.now()
                    thisPowerSession.powerOffPrice = currentEnergyPrice
                    thisPowerSession.endTemp = currentTemperature
                    elapsedSeconds = (thisPowerSession.powerOffTime - thisPowerSession.powerOnTime).total_seconds()
                    formatHours, formatSeconds = divmod(elapsedSeconds, 3600)
                    formatMinutes, formatSeconds = divmod(formatSeconds, 60)
                    elapsedTimeFormatted = f"{str(round(formatHours)).zfill(2)}:{str(round(formatMinutes)).zfill(2)}:{str(round(formatSeconds)).zfill(2)}"
                    sessionCost = 0
                    if thisPowerSession.powerOnTime != datetime.fromtimestamp(0):
                        if thisPowerSession.powerOnTime.hour == thisPowerSession.powerOffTime.hour:
                            sessionCost = estimateCost(elapsedSeconds, thisPowerSession.powerOffPrice)
                        elif thisPowerSession.powerOffTime.hour - thisPowerSession.powerOnTime.hour == 1 or thisPowerSession.powerOffTime.hour - thisPowerSession.powerOnTime.hour == -23:
                            interimTimestamp = datetime(thisPowerSession.powerOffTime.year, thisPowerSession.powerOffTime.month, thisPowerSession.powerOffTime.day, thisPowerSession.powerOffTime.hour, 0, 0)
                            elapsedSecondsFirst = (interimTimestamp - thisPowerSession.powerOnTime).total_seconds()
                            elapsedSecondsSecond = (thisPowerSession.powerOffTime - interimTimestamp).total_seconds()
                            sessionCostFirst = estimateCost(elapsedSecondsFirst, thisPowerSession.powerOnPrice)
                            sessionCostSecond = estimateCost(elapsedSecondsSecond, thisPowerSession.powerOffPrice)
                            sessionCost = sessionCostFirst + sessionCostSecond
                        else:
                            naivelyEstimatedPrice = (thisPowerSession.powerOffPrice + thisPowerSession.powerOnPrice) / 2
                            sessionCost = estimateCost(elapsedSeconds, naivelyEstimatedPrice)
                        syslogPrice(sessionCost, thisPowerSession.startTemp, thisPowerSession.endTemp, elapsedTimeFormatted)
                time.sleep(450) # Prevent equipment to flicker on/off too frequently

        except ConnectionError as e:
            syslog.syslog(syslog.LOG_ERR, f"ERROR: Failed connecting to the Philips Hue bridge: {e}")
        except Exception as e:
            syslog.syslog(syslog.LOG_ERR, f"ERROR: An unexpected error occurred connecting to the Philips Hue bridge: {e}")
    else:
        syslog.syslog(syslog.LOG_ERR, f"ERROR: Invalid input arguments (validation check in {inspect.currentframe().f_code.co_name})")



###########################################################################################################################################################
### main
###########################################################################################################################################################
syslog.openlog(ident="PyTempCmdr", logoption=syslog.LOG_PID, facility=syslog.LOG_LOCAL0)
syslog.syslog(syslog.LOG_INFO, "INFO: Startup initiated - Python Tibber Hue Temperature Commander in charge!")

print("\nCheck syslog for all logs after this initial startup info\n\n\
Examples:\n\n\
sudo tail -f /var/log/syslog | grep PyTempCmdr\n\
sudo grep PyTempCmdr /var/log/syslog | grep -i action\n\
sudo grep PyTempCmdr /var/log/syslog | less -i\n\n\n\
Run jobs command and use the correct job id to the disown command\n\
Example (where job number 1 is to be disowned):\n\n\
./pyTibberHueTempCommander.py &\n\
jobs\n\
disown -h %1\n\n\
If it appears like if you are not back at the shell's prompt, you probably need to press <ENTER>")

# Create and initiate 2-dim array, 0 = today, 1 = tomorrow
currentEnergyPrices = []
for i in range(2):
    currentEnergyPrices.append([])
    for j in range(24):
        currentEnergyPrices[i].append(0)

priceState = 0 # 0 = never fetched or Daylight Savings, 1 = only info about today's prices, 2 = info of both today's and tomorrow's prices
lastPriceRun = datetime.fromtimestamp(0)
thisPowerSession = PowerSession(datetime.fromtimestamp(0), datetime.fromtimestamp(0), 0, 0, 0, 0)

skipSleep = 0
while True:
    if skipSleep < 2:
        skipSleep += 1 # Startup performance
    else:
        time.sleep(151) # Ensure sleep every iteration of while loop, sleep first action in loop (e.g. if continue in the middle of the loop)
    
    currentTime = datetime.now()
    currentDate = currentTime.date()
    currentHour = currentTime.hour
    lastPriceRunDate = lastPriceRun.date()
    fetchNewEnergyPrices = False # unless explicitly needed (don't hammer Tibber's API)
    
    currentTemperature = getTemperature(philipsHueSensorName)
    if currentTemperature is None:
        syslog.syslog(syslog.LOG_ERR, "ERROR: Could not fetch temperature, skipping rest of logic for this while loop iteration (i.e. no action taken)")
        continue

    if priceState == 0: # init state (first time in the while loop or if daylight savings scenario)
        fetchNewEnergyPrices = True

    if priceState == 2 and currentDate > lastPriceRunDate: # time for switcheru (bubble tomorrow's prices into today's prices (first time in while loop after midnight))
        for i in range(24):
            currentEnergyPrices[0][i] = currentEnergyPrices[1][i]
            currentEnergyPrices[1][i] = 0
            priceState = 1
            
    if priceState == 1 and currentHour >= 13: # Tibber aims to publish prices at 13:00 (wait to while loop is after 13:00 to fetch tomorrow's prices)
        fetchNewEnergyPrices = True

    if currentHour == 23:
        nextEnergyPrice = currentEnergyPrices[1][0]
    else:
        nextEnergyPrice = currentEnergyPrices[0][currentHour+1]
    currentEnergyPrice = currentEnergyPrices[0][currentHour]
    todayLowPercentile = numpy.percentile(currentEnergyPrices[0], lowEnergyPricePercentileThreshold)
    todayHighPercentile = numpy.percentile(currentEnergyPrices[0], highEnergyPricePercentileThreshold)

    if currentTemperature < minTemperatureThreshold: # Always turn on the heat if MIN threshold has been reached, regardless of price
        syslog.syslog(syslog.LOG_INFO, f"INFO: Current temperature and price [{round(currentTemperature, 1)}] [{round(currentEnergyPrice, 2)}] | Temperature is lower than absolute min threshold [{minTemperatureThreshold}]")
        ensurePowerState(philipsHuePlugName, True, thisPowerSession, currentEnergyPrice, currentTemperature)
    elif currentTemperature > maxTemperatureThreshold: # Otherwise, always turn off the heat if MAX threshold has been reached, regardless of price
        syslog.syslog(syslog.LOG_INFO, f"INFO: Current temperature and price [{round(currentTemperature, 1)}] [{round(currentEnergyPrice, 2)}] | Temperature is higher than absolute max threshold [{maxTemperatureThreshold}]")
        ensurePowerState(philipsHuePlugName, False, thisPowerSession, currentEnergyPrice, currentTemperature)
    elif currentEnergyPrice < todayLowPercentile: # Otherwise, always turn on the heat if price is below the low percentile threshold, regardless of temperature
        syslog.syslog(syslog.LOG_INFO, f"INFO: Current temperature and price [{round(currentTemperature, 1)}] [{round(currentEnergyPrice, 2)}] | Current price [{round(currentEnergyPrice, 2)}] is lower than today's {lowEnergyPricePercentileThreshold}th percentile [{round(todayLowPercentile, 3)}]")
        ensurePowerState(philipsHuePlugName, True, thisPowerSession, currentEnergyPrice, currentTemperature)
    elif currentEnergyPrice > todayHighPercentile: # Otherwise, always turn off the heat if price is above high percentile threshold, regardless of temperature
        syslog.syslog(syslog.LOG_INFO, f"INFO: Current temperature and price [{round(currentTemperature, 1)}] [{round(currentEnergyPrice, 2)}] | Current price [{round(currentEnergyPrice, 2)}] is higher than today's {highEnergyPricePercentileThreshold}th percentile [{round(todayHighPercentile, 3)}]")
        ensurePowerState(philipsHuePlugName, False, thisPowerSession, currentEnergyPrice, currentTemperature)
    elif currentTemperature < normalMinTemperatureThreshold: # Otherwise, always turn on the heat if the normal threshold has been reached, regardless of price
        syslog.syslog(syslog.LOG_INFO, f"INFO: Current temperature and price [{round(currentTemperature, 1)}] [{round(currentEnergyPrice, 2)}] | Temperature is lower than normal min threshold [{normalMinTemperatureThreshold}]")
        ensurePowerState(philipsHuePlugName, True, thisPowerSession, currentEnergyPrice, currentTemperature)
    elif currentTemperature > normalMaxTemperatureThreshold: # Otherwise, always turn off the heat if the normal threshold has been reached, regardless of price
        syslog.syslog(syslog.LOG_INFO, f"INFO: Current temperature and price [{round(currentTemperature, 1)}] [{round(currentEnergyPrice, 2)}] | Temperature is higher than normal max threshold [{normalMaxTemperatureThreshold}]")
        ensurePowerState(philipsHuePlugName, False, thisPowerSession, currentEnergyPrice, currentTemperature)
    elif currentEnergyPrice < nextEnergyPrice: # Inside normal temperature range and normal price range --> Short-term greedy decision based on price right now versus next hour
        syslog.syslog(syslog.LOG_INFO, f"INFO: Current temperature and price [{round(currentTemperature, 1)}] [{round(currentEnergyPrice, 2)}] | Normal ranges and current price [{round(currentEnergyPrice, 2)}] is lower than next hour's price [{round(nextEnergyPrice, 3)}]")
        ensurePowerState(philipsHuePlugName, True, thisPowerSession, currentEnergyPrice, currentTemperature)
    elif currentEnergyPrice > nextEnergyPrice: # Other half of the greedy decision inside normal temperature range and normal price range
        syslog.syslog(syslog.LOG_INFO, f"INFO: Current temperature and price [{round(currentTemperature, 1)}] [{round(currentEnergyPrice, 2)}] | Normal ranges and current price [{round(currentEnergyPrice, 2)}] is higher than next hour's price [{round(nextEnergyPrice, 3)}]")
        ensurePowerState(philipsHuePlugName, False, thisPowerSession, currentEnergyPrice, currentTemperature)
    else:
        syslog.syslog(syslog.LOG_INFO, f"INFO: Current temperature [{round(currentTemperature, 1)}]")
        ensurePowerState(philipsHuePlugName, False, thisPowerSession, currentEnergyPrice, currentTemperature)

    if fetchNewEnergyPrices == True:
        try:
            energyPrices = getTodayAndTomorrowEnergyPrices()
            priceInfo = energyPrices["data"]["viewer"]["homes"][0]["currentSubscription"]["priceInfo"]
            if isinstance(priceInfo, dict) and len(priceInfo) == 2:
                today = priceInfo["today"]
                tomorrow = priceInfo["tomorrow"]
                daylightSavingsState = False
                thisObjects = [] # ensure this variable is initialized in case both expected scenarios fail prior to the for loop
                if isinstance(today, list) and isinstance(tomorrow, list) and len(today) == 24 and len(tomorrow) == 24:
                    thisObjects = [today, tomorrow]
                elif isinstance(today, list) and isinstance(tomorrow, list) and len(today) == 24 and len(tomorrow) == 23:
                    thisObjects = [today]
                elif isinstance(today, list) and isinstance(tomorrow, list) and len(today) == 24 and len(tomorrow) == 25:
                    thisObjects = [today]
                elif isinstance(today, list) and isinstance(tomorrow, list) and len(today) == 23 and len(tomorrow) == 24:
                    daylightSavingsState = True
                elif isinstance(today, list) and isinstance(tomorrow, list) and len(today) == 25 and len(tomorrow) == 24:
                    daylightSavingsState = True
                elif isinstance(today, list) and len(today) == 24:
                    thisObjects = [today]
                elif isinstance(today, list) and len(today) == 23:
                    daylightSavingsState = True
                elif isinstance(today, list) and len(today) == 25:
                    daylightSavingsState = True
                else:
                    syslog.syslog(syslog.LOG_ERR, "ERROR: Could not parse energy prices this wile loop iteration (today/tomorrow validation (i.e. no action taken))")
                    continue

                if daylightSavingsState == True:
                    backoffSleep = 600
                    syslog.syslog(syslog.LOG_ERR, f"ERROR: A scenario of Daylight savings occurred that is not yet supported by this app. This will resolve itself automatically on Monday. To avoid using Tibber's API too often, this app now pauses for {backoffSleep}s. Temperature control is now performed by either only using ABS MAX/MIN thresholds or by using yesterday's prices incorrectly.")
                    time.sleep(backoffSleep) # Avoid hammering Tibber's API
                    priceState = 0
                    continue

                priceArrayPopulated = False
                todayTomorrowIndex = -1
                for day in thisObjects:
                    todayTomorrowIndex += 1
                    for keyValue in day:
                        if isinstance(keyValue, dict) and len(keyValue) == 2 and "total" in keyValue and "startsAt" in keyValue:
                            totalPrice = keyValue["total"]
                            timeStartHourDatetime = keyValue["startsAt"]
                            if isinstance(totalPrice, float) and isinstance(timeStartHourDatetime, str):
                                thisDatetime = datetime.fromisoformat(timeStartHourDatetime)
                                currentEnergyPrices[todayTomorrowIndex][thisDatetime.hour] = totalPrice
                                priceArrayPopulated = True
                                lastPriceRun = currentTime
                                priceState = todayTomorrowIndex + 1
                            else:
                                syslog.syslog(syslog.LOG_ERR, "ERROR: Not expected price format received (key/value pair)")
                        else:
                            syslog.syslog(syslog.LOG_ERR, "ERROR: Not expected price format received (dictionary)")
                if priceArrayPopulated == False:
                    syslog.syslog(syslog.LOG_ERR, "ERROR: No prices fetched/populated this while loop")
            else:
                syslog.syslog(syslog.LOG_ERR, "ERROR: Could not parse energy prices this while loop (price info dictionary validation)")

        except Exception as e:
            syslog.syslog(syslog.LOG_ERR, f"ERROR: When fetching energy prices (but continuing using old prices). Details: {e}")

syslog.closelog()
