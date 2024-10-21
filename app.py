import os
import re
import shlex
import sqlite3
import subprocess
import threading
import time
import json
import requests
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from flask_basicauth import BasicAuth

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_KEY')
app.config['BASIC_AUTH_USERNAME'] = os.getenv('ADMIN_USER')
app.config['BASIC_AUTH_PASSWORD'] = os.getenv('ADMIN_PASS')
basic_auth = BasicAuth(app)

DATABASE = f"pochta.db"


def get_db_connection():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def evaluate_script(data, script):
    try:
        return eval(script, {"__builtins__": None}, data)
    except Exception as e:
        print(f"Error evaluating script: {e}")
        return False


def check_status_for_service(service, timeout=30):
    try:
        start_time = time.time()
        curl_command = decode_curl_command(service['curl_command'])
        args = shlex.split(curl_command, posix=True)
        args += ['-i', '-s']
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        end_time = time.time()

        split_result = re.split(r'\r?\n\r?\n', result.stdout, maxsplit=1)
        headers = split_result[0]
        response_body = split_result[1] if len(split_result) > 1 else ""

        headers_lines = re.split(r'\r?\n', headers)
        status_line = headers_lines[0]
        http_code = int(status_line.split()[1])

        response_headers = {}
        for header in headers_lines[1:]:
            if ':' in header:
                key, value = header.split(':', 1)
                response_headers[key.strip()] = value.strip()

        timespent = end_time - start_time
        is_up = evaluate_script({
            'timespent': timespent,
            'response_code': http_code,
            'response_body': response_body,
            'response_headers': response_headers,
        }, service['script'])
        response_time = result.stderr.strip()

    except subprocess.TimeoutExpired:
        is_up = False
        response_body = ""
        http_code = None
        response_headers = {}
        response_time = None
        timespent = timeout

    return {
        'name': service['name'],
        'is_up': is_up,
        'timestamp': time.time(),
        'timespent': timespent,
        'response_code': http_code,
        'response_body': response_body,
        'response_headers': response_headers,
        'response_time': response_time
    }


def monitor_services():
    while True:
        conn = get_db_connection()
        services = conn.execute('SELECT * FROM services').fetchall()

        for service in services:
            status_data = check_status_for_service(service)

            if not status_data['is_up']:
                uptime_logs = conn.execute(
                    'SELECT timestamp FROM uptimes WHERE service_id = ? AND status = 1 ORDER BY timestamp DESC LIMIT 1',
                    (service['id'],)
                ).fetchone()

                last_up = uptime_logs['timestamp'] if uptime_logs else None

                if last_up == None:
                    continue # Skipping services that was never alive to not distrube

                message = (
                    f"Service {service['name']} is down!\n"
                    f"Last checked: {time.ctime(status_data['timestamp'])}\n"
                    f"Last up: {time.ctime(last_up) if last_up else 'Unknown'}"
                )
                requests.post(os.getenv('WEBHOOK_URL'), json={"text": message})

            log_uptime(conn, service['id'], int(status_data['is_up']))

        conn.commit()
        conn.close()
        time.sleep(int(os.getenv('CHECK_INTERVAL')))


def log_uptime(conn, service_id, status):
    conn.execute('INSERT INTO uptimes (service_id, status, timestamp) VALUES (?, ?, CURRENT_TIMESTAMP)',
                 (service_id, status))


def encode_curl_command(command):
    return json.dumps(command)


def decode_curl_command(encoded_command):
    decoded = json.loads(encoded_command).strip('"')
    return f"{decoded}"


@app.context_processor
def utility_processor():
    def custom_zip(*args, **kwargs):
        return zip(*args, **kwargs)

    return dict(zip=custom_zip)


@app.route('/')
def status_page():
    conn = get_db_connection()
    services_with_status = conn.execute('''
        SELECT services.id, services.name, lu.status AS status
        FROM services
        LEFT JOIN (
            SELECT service_id, status, MAX(timestamp) AS LatestTime
            FROM uptimes
            GROUP BY service_id
        ) AS lu ON services.id = lu.service_id
    ''').fetchall()
    conn.close()
    return render_template('index.html', services=services_with_status)


@app.route('/history')
def history_page():
    conn = get_db_connection()

    # Определяем количество записей на странице
    per_page = 10
    page = request.args.get('page', 1, type=int)
    filter_service = request.args.get('service', '', type=str)
    filter_status = request.args.get('status', '', type=str)

    # Подсчитываем общее количество записей (учитывая фильтры)
    base_query = 'SELECT COUNT(*) FROM uptimes JOIN services ON uptimes.service_id = services.id WHERE 1=1'
    filters = []
    if filter_service:
        base_query += ' AND services.name = ?'
        filters.append(filter_service)
    if filter_status:
        base_query += ' AND uptimes.status = ?'
        filters.append(int(filter_status))

    total_services = conn.execute(base_query, filters).fetchone()[0]
    total_pages = (total_services + per_page - 1) // per_page

    # Получаем записи для текущей страницы (учитывая фильтры)
    query = '''
        SELECT services.name, uptimes.timestamp, uptimes.status
        FROM uptimes
        JOIN services ON uptimes.service_id = services.id
        WHERE 1=1
    '''
    if filter_service:
        query += ' AND services.name = ?'
    if filter_status:
        query += ' AND uptimes.status = ?'

    query += ' ORDER BY uptimes.timestamp DESC LIMIT ? OFFSET ?'
    uptimes = conn.execute(query, filters + [per_page, (page - 1) * per_page]).fetchall()

    # Получаем уникальные имена сервисов для фильтра
    unique_services = conn.execute('SELECT DISTINCT name FROM services').fetchall()

    conn.close()

    # Параметры для сохранения текущего выборочного состояния
    filter_params = {
        'service': filter_service,
        'status': filter_status
    }

    return render_template('history.html', uptimes=uptimes, page=page, total_pages=total_pages,
                           unique_services=unique_services, filter_params=filter_params)


@app.route('/admin', methods=['GET'])
@basic_auth.required
def admin_page():
    conn = get_db_connection()
    services = conn.execute('SELECT * FROM services').fetchall()
    conn.close()
    return render_template('admin.html', services=services)


@app.route('/add_service', methods=['POST'])
@basic_auth.required
def add_service():
    name = request.form['name']
    curl_command = request.form['curl_command']
    encoded_command = encode_curl_command(curl_command)
    script = request.form['script']

    conn = get_db_connection()
    conn.execute('INSERT INTO services (name, curl_command, script) VALUES (?, ?, ?)',
                 (name, encoded_command, script))
    conn.commit()
    conn.close()
    return redirect(url_for('admin_page'))


@app.route('/edit_service/<int:service_id>', methods=['GET', 'POST'])
@basic_auth.required
def edit_service(service_id):
    conn = get_db_connection()
    if request.method == 'POST':
        name = request.form['name']
        curl_command = request.form['curl_command']
        encoded_command = encode_curl_command(curl_command)
        script = request.form['script']

        conn.execute('UPDATE services SET name = ?, curl_command = ?, script = ? WHERE id = ?',
                     (name, encoded_command, script, service_id))
        conn.commit()
        conn.close()
        return redirect(url_for('admin_page'))

    service = conn.execute('SELECT * FROM services WHERE id = ?', (service_id,)).fetchone()
    decoded_command = decode_curl_command(service['curl_command'])
    conn.close()
    return render_template('edit.html', service=service, decoded_command=decoded_command)


@app.route('/delete_service/<int:service_id>', methods=['POST'])
@basic_auth.required
def delete_service(service_id):
    conn = get_db_connection()
    conn.execute('DELETE FROM services WHERE id = ?', (service_id,))
    conn.commit()
    conn.close()
    return redirect(url_for('admin_page'))


@app.route('/run_tests', methods=['POST'])
@basic_auth.required
def run_tests():
    conn = get_db_connection()
    services = conn.execute('SELECT * FROM services').fetchall()
    data_total = []

    for service in services:
        status_data = check_status_for_service(service)

        if not status_data['is_up']:
            uptime_logs = conn.execute(
                'SELECT timestamp FROM uptimes WHERE service_id = ? AND status = 1 ORDER BY timestamp DESC LIMIT 1',
                (service['id'],)
            ).fetchone()

            last_up = uptime_logs['timestamp'] if uptime_logs else None

            if last_up == None:
                continue  # Skipping services that was never alive to not distrube

            message = (
                f"Service {service['name']} is down!\n"
                f"Last checked: {time.ctime(status_data['timestamp'])}\n"
                f"Last up: {time.ctime(last_up) if last_up else 'Unknown'}"
            )
            requests.post(os.getenv('WEBHOOK_URL'), json={"text": message})

        log_uptime(conn, service['id'], int(status_data['is_up']))
        data_total.append(status_data)
    return jsonify(data_total)
    # return redirect(url_for('status_page'))


conn = get_db_connection()
conn.execute('''CREATE TABLE IF NOT EXISTS services (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                curl_command TEXT NOT NULL,
                script TEXT NOT NULL)''')
conn.execute('''CREATE TABLE IF NOT EXISTS uptimes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                service_id INTEGER NOT NULL,
                status INTEGER NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL)''')
conn.close()

threading.Thread(target=monitor_services, daemon=True).start()

app.run(host=os.getenv('APP_HOST', '0.0.0.0'), port=os.getenv('APP_PORT', 5000))
