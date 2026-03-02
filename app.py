import os
import asyncio
import threading
import json
import time
from flask import Flask, render_template, request, jsonify, session
from flask_socketio import SocketIO, emit
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError, FloodWaitError
from telethon.tl.types import Channel, Chat
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.urandom(24)
app.config['SECRET_KEY'] = os.urandom(24)

socketio = SocketIO(app, async_mode='eventlet', cors_allowed_origins='*')

# Global state
clients = {}         # sid -> TelegramClient
broadcast_tasks = {} # sid -> running flag
loop = None

def get_or_create_loop():
    global loop
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop

def run_async(coro):
    lp = get_or_create_loop()
    if lp.is_running():
        future = asyncio.run_coroutine_threadsafe(coro, lp)
        return future.result(timeout=60)
    else:
        return lp.run_until_complete(coro)

# ─── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/send-code', methods=['POST'])
def send_code():
    data = request.json
    api_id   = int(data.get('api_id'))
    api_hash = data.get('api_hash')
    phone    = data.get('phone')
    sid      = data.get('sid')

    session_file = f"sessions/{phone.replace('+','')}"
    os.makedirs('sessions', exist_ok=True)

    try:
        client = TelegramClient(session_file, api_id, api_hash)
        clients[sid] = client

        async def do_connect():
            await client.connect()
            if await client.is_user_authorized():
                return {'status': 'already_logged_in'}
            result = await client.send_code_request(phone)
            return {'status': 'code_sent', 'phone_code_hash': result.phone_code_hash}

        result = run_async(do_connect())
        session['phone'] = phone
        return jsonify(result)
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400

@app.route('/api/verify-code', methods=['POST'])
def verify_code():
    data  = request.json
    phone = data.get('phone')
    code  = data.get('code')
    sid   = data.get('sid')
    password = data.get('password', '')

    client = clients.get(sid)
    if not client:
        return jsonify({'status': 'error', 'message': 'No client session found'}), 400

    try:
        async def do_sign_in():
            try:
                await client.sign_in(phone, code)
            except SessionPasswordNeededError:
                if password:
                    await client.sign_in(password=password)
                else:
                    return {'status': 'need_password'}
            me = await client.get_me()
            return {'status': 'success', 'name': me.first_name, 'username': me.username}

        result = run_async(do_sign_in())
        return jsonify(result)
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400

@app.route('/api/get-groups', methods=['POST'])
def get_groups():
    data = request.json
    sid  = data.get('sid')
    client = clients.get(sid)
    if not client:
        return jsonify({'status': 'error', 'message': 'Not authenticated'}), 401

    try:
        async def fetch_groups():
            dialogs = await client.get_dialogs()
            groups = []
            for d in dialogs:
                entity = d.entity
                if isinstance(entity, (Channel, Chat)):
                    groups.append({
                        'id': entity.id,
                        'name': d.name,
                        'type': 'channel' if isinstance(entity, Channel) else 'group',
                        'members': getattr(entity, 'participants_count', '?'),
                    })
            return groups

        groups = run_async(fetch_groups())
        return jsonify({'status': 'success', 'groups': groups})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400

@app.route('/api/start-broadcast', methods=['POST'])
def start_broadcast():
    data         = request.json
    sid          = data.get('sid')
    group_ids    = data.get('group_ids', [])
    message      = data.get('message', '')
    delay        = int(data.get('delay', 5))
    auto_repeat  = data.get('auto_repeat', False)
    repeat_mins  = int(data.get('repeat_minutes', 60))
    parse_mode   = data.get('parse_mode', 'markdown')

    client = clients.get(sid)
    if not client:
        return jsonify({'status': 'error', 'message': 'Not authenticated'}), 401

    broadcast_tasks[sid] = True

    def run_broadcast():
        asyncio.set_event_loop(asyncio.new_event_loop())
        lp = asyncio.get_event_loop()

        async def broadcast():
            round_num = 1
            while broadcast_tasks.get(sid):
                socketio.emit('broadcast_log', {
                    'type': 'info',
                    'message': f'🔄 Starting round #{round_num}...'
                }, room=sid)

                for gid in group_ids:
                    if not broadcast_tasks.get(sid):
                        break
                    try:
                        entity = await client.get_entity(int(gid))
                        kwargs = {'parse_mode': parse_mode}
                        await client.send_message(entity, message, **kwargs)
                        socketio.emit('broadcast_log', {
                            'type': 'success',
                            'message': f'✅ Sent to {entity.title}',
                            'group': entity.title
                        }, room=sid)
                    except FloodWaitError as e:
                        socketio.emit('broadcast_log', {
                            'type': 'warning',
                            'message': f'⚠️ Flood wait {e.seconds}s for group {gid}'
                        }, room=sid)
                        await asyncio.sleep(e.seconds)
                    except Exception as e:
                        socketio.emit('broadcast_log', {
                            'type': 'error',
                            'message': f'❌ Failed group {gid}: {str(e)}'
                        }, room=sid)

                    if broadcast_tasks.get(sid):
                        await asyncio.sleep(delay)

                socketio.emit('broadcast_log', {
                    'type': 'info',
                    'message': f'✔ Round #{round_num} complete.'
                }, room=sid)

                if not auto_repeat or not broadcast_tasks.get(sid):
                    break

                socketio.emit('broadcast_log', {
                    'type': 'info',
                    'message': f'⏳ Waiting {repeat_mins} minutes before next round...'
                }, room=sid)
                await asyncio.sleep(repeat_mins * 60)
                round_num += 1

            socketio.emit('broadcast_done', {'message': 'Broadcast finished.'}, room=sid)

        lp.run_until_complete(broadcast())

    t = threading.Thread(target=run_broadcast, daemon=True)
    t.start()
    return jsonify({'status': 'started'})

@app.route('/api/stop-broadcast', methods=['POST'])
def stop_broadcast():
    data = request.json
    sid  = data.get('sid')
    broadcast_tasks[sid] = False
    return jsonify({'status': 'stopped'})

@app.route('/api/logout', methods=['POST'])
def logout():
    data = request.json
    sid  = data.get('sid')
    client = clients.get(sid)
    if client:
        async def do_logout():
            await client.log_out()
            await client.disconnect()
        try:
            run_async(do_logout())
        except:
            pass
        del clients[sid]
    return jsonify({'status': 'logged_out'})

# ─── SocketIO ──────────────────────────────────────────────────────────────────

@socketio.on('connect')
def on_connect():
    logger.info(f'Client connected: {request.sid}')

@socketio.on('disconnect')
def on_disconnect():
    broadcast_tasks[request.sid] = False
    logger.info(f'Client disconnected: {request.sid}')

if __name__ == '__main__':
    os.makedirs('sessions', exist_ok=True)
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)
