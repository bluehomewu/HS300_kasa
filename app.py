import asyncio
import random
import schedule
import time
import logging
from datetime import datetime
from quart import Quart, render_template, jsonify, request, websocket
from kasa.iot import IotStrip  # 改用新版 IotStrip
from timezonefinder import TimezoneFinder
import configparser
import os
import mysql.connector
import threading

logging.basicConfig(level=logging.INFO)

# 從 database.config 讀取資料庫連線資訊
if os.path.exists("database.config"):
    db_config = configparser.ConfigParser()
    db_config.read("database.config")
    db_params = db_config["database"]
else:
    raise Exception("database.config file not found!")

# 從 device.config 讀取設備相關資訊（包含 IP 與 wifi_ssid）
if os.path.exists("device.config"):
    device_conf = configparser.ConfigParser()
    device_conf.read("device.config")
    # 建立 devices 字典，key 為 section 名稱，value 為該設備的設定（包含 ip 與 wifi_ssid 等）
    devices = {section: dict(device_conf[section]) for section in device_conf.sections()}
    ips = [devices[dev]["ip"] for dev in devices]
else:
    ips = input("Enter the IP addresses of your HS300 (comma-separated): ").split(", ")
    devices = {}
    for ip in ips:
        devices[ip] = {"ip": ip, "wifi_ssid": ""}


class HS300Controller:
    def __init__(self, ip_address, wifi_ssid=None):
        self.ip_address = ip_address
        self.strip = IotStrip(ip_address)
        self.tf = TimezoneFinder()
        self.schedules = {}  # 排程任務存放 (索引對應的 schedule job 清單)
        self.lock = asyncio.Lock()  # 保證非同步操作的互斥
        self.cached_plug_info = None  # 用來存放最新的插座狀態資料
        # 儲存來自 device.config 的 WiFi SSID（若有設定）
        self.wifi_ssid = wifi_ssid
        # 使用從 database.config 讀取的連線資訊建立資料庫連線
        self.db_conn = mysql.connector.connect(
            host=db_params.get("host", "localhost"),
            user=db_params.get("user", ""),
            password=db_params.get("password", ""),
            database=db_params.get("database", "")
        )
        self.cursor = self.db_conn.cursor()
        self.setup_db()
        self.groups = self.load_groups()

    def setup_db(self):
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS group_list (
                name VARCHAR(255) PRIMARY KEY,
                sockets TEXT,
                controller_ip VARCHAR(255)
            )
        ''')
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS plug_data (
                id INT AUTO_INCREMENT PRIMARY KEY,
                controller_ip VARCHAR(255),
                plug_index INT,
                alias VARCHAR(255),
                state VARCHAR(10),
                voltage FLOAT,
                current FLOAT,
                power FLOAT,
                total_energy FLOAT,
                on_since DATETIME,
                today_consumption FLOAT,
                month_consumption FLOAT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        self.db_conn.commit()

    def load_groups(self):
        self.cursor.execute('SELECT name, sockets, controller_ip FROM group_list')
        rows = self.cursor.fetchall()
        groups = {name: (sockets.split(','), controller_ip) for name, sockets, controller_ip in rows}
        return groups

    def create_group(self, name, indexes, ip):
        if name in self.groups:
            raise ValueError(f"Group {name} already exists")
        aliases = [self.strip.children[int(index)].alias for index in indexes]
        self.groups[name] = (aliases, ip)
        self.cursor.execute(
            'INSERT INTO group_list (name, sockets, controller_ip) VALUES (%s, %s, %s)',
            (name, ','.join(aliases), ip)
        )
        self.db_conn.commit()

    def delete_group(self, name):
        if name in self.groups:
            del self.groups[name]
            self.cursor.execute('DELETE FROM group_list WHERE name = %s', (name,))
            self.db_conn.commit()

    def rename_group(self, old_name, new_name):
        if old_name not in self.groups:
            raise ValueError(f"Group {old_name} does not exist")
        if new_name in self.groups:
            raise ValueError(f"Group {new_name} already exists")
        self.groups[new_name] = self.groups.pop(old_name)
        self.cursor.execute('UPDATE group_list SET name = %s WHERE name = %s', (new_name, old_name))
        self.db_conn.commit()

    async def update_strip(self):
        await self.strip.update()

    async def get_plug_info(self):
        async with self.lock:
            await self.update_strip()
            plugs = []
            for i, plug in enumerate(self.strip.children):
                try:
                    # 注意：get_emeter_realtime 已被標記為棄用，建議參考新版用法
                    emeter = await plug.get_emeter_realtime()
                except Exception as e:
                    logging.error(f"Error getting emeter for plug {i}: {e}")
                    emeter = {"voltage_mv": 0, "current_ma": 0, "power_mw": 0, "total_wh": 0}
                plug_info = {
                    "index": i,
                    "alias": plug.alias,
                    "state": "On" if plug.is_on else "Off",
                    "voltage": emeter.get('voltage_mv', 0) / 1000,
                    "current": emeter.get('current_ma', 0) / 1000,
                    "power": emeter.get('power_mw', 0) / 1000,
                    "total_energy": emeter.get('total_wh', 0) / 1000,
                    "on_since": getattr(plug, 'on_since', None),
                    "today_consumption": emeter.get('total_wh', 0) / 1000,
                    "month_consumption": emeter.get('total_wh', 0) / 1000,
                }
                plugs.append(plug_info)
                self.cursor.execute('''
                    INSERT INTO plug_data (controller_ip, plug_index, alias, state, voltage, current, power, total_energy, on_since, today_consumption, month_consumption)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ''', (
                    self.ip_address, i, plug.alias, plug_info["state"],
                    plug_info["voltage"], plug_info["current"],
                    plug_info["power"], plug_info["total_energy"],
                    plug_info["on_since"], plug_info["today_consumption"],
                    plug_info["month_consumption"]
                ))
                self.db_conn.commit()
            return plugs

    async def update_cache(self):
        while True:
            try:
                self.cached_plug_info = await self.get_plug_info()
            except Exception as e:
                logging.error(f"Error updating cache for {self.ip_address}: {e}")
            await asyncio.sleep(3)

    async def get_device_info(self):
        await self.update_strip()
        sys_info = self.strip.sys_info
        device_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # 若有從 device.config 中設定 WiFi SSID，則使用之；否則以系統資訊為主
        wifi_ssid = self.wifi_ssid if self.wifi_ssid else sys_info.get("ssid", "LAB51")
        latitude = sys_info.get("latitude_i", 0) / 10000.0
        longitude = sys_info.get("longitude_i", 0) / 10000.0
        region = f"Lat: {latitude}, Lon: {longitude}"
        timezone = self.tf.timezone_at(lng=longitude, lat=latitude)
        return {
            "timezone": timezone,
            "region": region,
            "device_time": sys_info.get("time", device_time),
            "wifi_network": wifi_ssid,
            "model": sys_info.get("model"),
            "hardware_version": sys_info.get("hw_ver"),
            "firmware_version": sys_info.get("sw_ver"),
            "mac_address": sys_info.get("mac")
        }

    async def toggle_plug(self, plug):
        async with self.lock:
            if plug.is_on:
                await plug.turn_off()
                logging.info(f"Plug {plug.alias} turned OFF")
            else:
                await plug.turn_on()
                logging.info(f"Plug {plug.alias} turned ON")

    async def random_toggle(self, plug_index):
        await self.update_strip()
        plug = self.strip.children[plug_index]
        logging.info(f"Randomly toggling plug {plug.alias}")
        await self.toggle_plug(plug)

    async def execute_schedules(self):
        def run_sched():
            while True:
                schedule.run_pending()
                time.sleep(1)
        t = threading.Thread(target=run_sched, daemon=True)
        t.start()

    async def toggle_group(self, name, action):
        if name not in self.groups:
            raise ValueError(f"Group {name} does not exist")
        aliases, ip = self.groups[name]
        plugs_to_toggle = [plug for plug in self.strip.children if plug.alias in aliases]
        for plug in plugs_to_toggle:
            if action == 'on' and not plug.is_on:
                await plug.turn_on()
            elif action == 'off' and plug.is_on:
                await plug.turn_off()

    def get_group_status(self, name):
        if name not in self.groups:
            raise ValueError(f"Group {name} does not exist")
        total_power = 0
        for alias in self.groups[name][0]:
            plug = next((plug for plug in self.strip.children if plug.alias == alias), None)
            if plug:
                try:
                    emeter = plug.emeter_realtime
                    total_power += emeter.get('power_mw', 0) / 1000
                except Exception as e:
                    logging.error(f"Error reading emeter for {plug.alias}: {e}")
        return total_power


# 建立控制器，並將該設備的 wifi_ssid 傳入
controllers = {}
for dev in devices:
    ip = devices[dev]["ip"]
    wifi_ssid = devices[dev].get("wifi_ssid", "")
    controllers[ip] = HS300Controller(ip, wifi_ssid=wifi_ssid)

app = Quart(__name__)

@app.before_serving
async def startup():
    # —— 新增：先檢查哪些 controller 連不上，記錄 offline IP —— 
    offline = []
    for ip, controller in list(controllers.items()):
        try:
            # 嘗試最小量更新，只查 sys_info
            await controller.strip.update()
        except Exception as e:
            logging.warning(f"裝置 {ip} 離線: {e}")
            offline.append(ip)
    # 把離線的從 controllers 中移除，之後只對在線的做排程與快取
    for ip in offline:
        controllers.pop(ip)
    # 將離線清單存到 app
    app.offline_ips = offline

    # —— 原本：啟動排程與快取 —— 
    for controller in controllers.values():
        asyncio.create_task(controller.execute_schedules())
        asyncio.create_task(controller.update_cache())
    # 原本：最後一次全量 update_strip()
    await asyncio.gather(*(controller.update_strip() for controller in controllers.values()))

@app.route('/')
async def index():
    plugs = {}
    device_info = {}
    for ip, controller in controllers.items():
        plugs[ip] = controller.cached_plug_info if controller.cached_plug_info is not None else []
        device_info[ip] = await controller.get_device_info()
    offline = getattr(app, 'offline_ips', [])
    return await render_template('index.html', controllers=list(controllers.keys()), plugs=plugs, device_info=device_info, ips=list(controllers.keys()), offline_ips=offline)

@app.route('/data/<ip>')
async def data(ip):
    controller = controllers.get(ip)
    if not controller:
        return jsonify({"status": "error", "message": f"Controller for IP {ip} not found"})
    return jsonify(controller.cached_plug_info or [])

@app.route('/toggle/<ip>/<int:plug_index>', methods=['POST'])
async def toggle(ip, plug_index):
    controller = controllers.get(ip)
    if not controller:
        return jsonify({"status": "error", "message": f"Controller for IP {ip} not found"})
    try:
        plug = controller.strip.children[plug_index]
        await controller.toggle_plug(plug)
        return jsonify({"status": "success", "message": "Toggled plug successfully"})
    except Exception as e:
        logging.error(f"Toggle error: {e}")
        return jsonify({"status": "error", "message": str(e)})

@app.route('/schedule', methods=['POST'])
async def schedule_plug():
    data = await request.json
    ip = data['ip']
    index = data['index']
    action = data['action']
    time_str = data['time']
    days = data['days']
    controller = controllers.get(ip)
    if not controller:
        return jsonify({"status": "error", "message": f"Controller for IP {ip} not found"})
    if index not in controller.schedules:
        controller.schedules[index] = []
    for day in days:
        day_lower = day.lower()
        if action == "on":
            job = getattr(schedule.every(), day_lower).at(time_str).do(lambda idx=index: asyncio.run(controller.strip.children[idx].turn_on()))
        else:
            job = getattr(schedule.every(), day_lower).at(time_str).do(lambda idx=index: asyncio.run(controller.strip.children[idx].turn_off()))
        controller.schedules[index].append(job)
    return jsonify({"status": "success"})

@app.route('/timer', methods=['POST'])
async def timer():
    data = await request.json
    ip = data['ip']
    index = data['index']
    duration = data['duration']
    controller = controllers.get(ip)
    if not controller:
        return jsonify({"status": "error", "message": f"Controller for IP {ip} not found"})
    plug = controller.strip.children[index]
    async def timer_task():
        await asyncio.sleep(duration)
        await controller.toggle_plug(plug)
    asyncio.create_task(timer_task())
    return jsonify({"status": "success"})

@app.route('/rename', methods=['POST'])
async def rename_plug():
    data = await request.json
    ip = data['ip']
    index = data['index']
    new_alias = data['new_alias']
    controller = controllers.get(ip)
    if not controller:
        return jsonify({"status": "error", "message": f"Controller for IP {ip} not found"})
    try:
        plug = controller.strip.children[index]
        await plug.set_alias(new_alias)
        return jsonify({"status": "success"})
    except Exception as e:
        logging.error(f"Rename plug error: {e}")
        return jsonify({"status": "error", "message": str(e)})

@app.route('/all_on', methods=['POST'])
async def all_on():
    for ip, controller in controllers.items():
        await controller.update_strip()
        plugs_to_turn_on = [plug for plug in controller.strip.children if not plug.is_on]
        for plug in plugs_to_turn_on:
            await plug.turn_on()
    return jsonify({"status": "success", "message": "Turned on all plugs (global)"})

@app.route('/all_off', methods=['POST'])
async def all_off():
    for ip, controller in controllers.items():
        await controller.update_strip()
        plugs_to_turn_off = [plug for plug in controller.strip.children if plug.is_on]
        for plug in plugs_to_turn_off:
            await plug.turn_off()
    return jsonify({"status": "success", "message": "Turned off all plugs (global)"})

# 新增個別設備的全部開啟
@app.route('/device_on/<ip>', methods=['POST'])
async def device_on(ip):
    controller = controllers.get(ip)
    if not controller:
        return jsonify({"status": "error", "message": f"Controller for IP {ip} not found"})
    await controller.update_strip()
    plugs_to_turn_on = [plug for plug in controller.strip.children if not plug.is_on]
    for plug in plugs_to_turn_on:
        await plug.turn_on()
    return jsonify({"status": "success", "message": f"Turned on all plugs for device {ip}"})

# 新增個別設備的全部關閉
@app.route('/device_off/<ip>', methods=['POST'])
async def device_off(ip):
    controller = controllers.get(ip)
    if not controller:
        return jsonify({"status": "error", "message": f"Controller for IP {ip} not found"})
    await controller.update_strip()
    plugs_to_turn_off = [plug for plug in controller.strip.children if plug.is_on]
    for plug in plugs_to_turn_off:
        await plug.turn_off()
    return jsonify({"status": "success", "message": f"Turned off all plugs for device {ip}"})

@app.route('/create_group', methods=['POST'])
async def create_group():
    data = await request.json
    name = data['name']
    indexes = data['indexes']
    ip = data['ip']
    controller = controllers.get(ip)
    if not controller:
        return jsonify({"status": "error", "message": f"Controller for IP {ip} not found"})
    try:
        controller.create_group(name, indexes, ip)
        return jsonify({"status": "success", "message": f"Group '{name}' created successfully"})
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route('/delete_group', methods=['POST'])
async def delete_group():
    data = await request.json
    name = data['name']
    ip = data['ip']
    controller = controllers.get(ip)
    if not controller:
        return jsonify({"status": "error", "message": f"Controller for IP {ip} not found"})
    try:
        controller.delete_group(name)
        return jsonify({"status": "success", "message": f"Group '{name}' deleted successfully"})
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route('/rename_group', methods=['POST'])
async def rename_group():
    data = await request.json
    old_name = data['old_name']
    new_name = data['new_name']
    ip = data['ip']
    controller = controllers.get(ip)
    if not controller:
        return jsonify({"status": "error", "message": f"Controller for IP {ip} not found"})
    try:
        controller.rename_group(old_name, new_name)
        return jsonify({"status": "success", "message": f"Group '{old_name}' renamed to '{new_name}' successfully"})
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route('/toggle_group', methods=['POST'])
async def toggle_group():
    data = await request.json
    name = data['name']
    action = data['action']
    ip = data['ip']
    controller = controllers.get(ip)
    if not controller:
        return jsonify({"status": "error", "message": f"Controller for IP {ip} not found"})
    try:
        await controller.toggle_group(name, action)
        return jsonify({"status": "success", "message": f"Group '{name}' toggled {action} successfully"})
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route('/group_status/<name>/<ip>', methods=['GET'])
async def group_status(name, ip):
    controller = controllers.get(ip)
    if not controller:
        return jsonify({"status": "error", "message": f"Controller for IP {ip} not found"})
    try:
        total_power = controller.get_group_status(name)
        return jsonify({"status": "success", "total_power": total_power})
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route('/groups/<ip>', methods=['GET'])
async def groups(ip):
    controller = controllers.get(ip)
    if not controller:
        return jsonify({"status": "error", "message": f"Controller for IP {ip} not found"})
    group_list = [{"name": name, "sockets": sockets} for name, (sockets, controller_ip) in controller.groups.items() if controller_ip == ip]
    return jsonify({"groups": group_list})

@app.route('/groups_all', methods=['GET'])
async def groups_all():
    conn = mysql.connector.connect(
        host=db_params.get("host", "localhost"),
        user=db_params.get("user", ""),
        password=db_params.get("password", ""),
        database=db_params.get("database", "")
    )
    cursor = conn.cursor()
    cursor.execute('SELECT name, sockets, controller_ip FROM group_list')
    rows = cursor.fetchall()
    groups = [
        {"name": name, "sockets": sockets.split(','), "controller_ip": controller_ip}
        for name, sockets, controller_ip in rows
    ]
    cursor.close()
    conn.close()
    return jsonify({"groups": groups})

# WebSocket 端點：推播快取資料給前端
@app.websocket('/ws/<ip>')
async def ws(ip):
    controller = controllers.get(ip)
    if not controller:
        return
    while True:
        if controller.cached_plug_info:
            await websocket.send_json(controller.cached_plug_info)
        await asyncio.sleep(3)

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000)
