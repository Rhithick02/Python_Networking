import os
import rsa
import json
import time
import string
import socket
import shutil
import random
import base64
import asyncio
import datetime
import traceback
import websockets
import tinymongo as tm
import tinydb

from tinymongo import TinyMongoClient
from filemanage import get_hash
from werkzeug.security import generate_password_hash, check_password_hash

# Minor change to tinymongo for python 3.8 version
class TinyMongoClient(tm.TinyMongoClient):
    @property
    def _storage(self):
        return tinydb.storages.JSONStorage

ok = False
my_NAME = socket.gethostname()
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
    s.connect(('8.8.8.8', 53))
    my_IP = s.getsockname()[0]

print(my_NAME, my_IP)

__client__ = TinyMongoClient(os.path.join(os.getcwd(), 'data.db'))
db = __client__['data']
shared_files = db.Shares.find()
SETTINGS = db.Settings.find_one({'_id': 'settings'})
CONNECTIONS = set()

class ConnectionHandler:
    websocket = None
    hostname = None #Of the connected machine
    uri = None
    state = 'Disconnected'
    shares = None
    peers = None
    async def send(self, message):
        try:
            data = json.dumps(message)
            await self.websocket.send(data)
        except:
            traceback.print_exc()

    async def recv(self):
        try:
            message = await self.websocket.recv()
            data = json.loads(message)
            return data
        except:
            traceback.print_exc()
    
    async def file_send(self, filepath):
        finish = False
        self.state = 'Transfer'
        with open(filepath, 'rb') as f:
            while not finish:
                buffer = f.read(8192)
                if not buffer:
                    buffer = ':EOF'
                    finish = True
                print("Send chunk")
                await self.websocket.send(buffer)                
                await asyncio.sleep(0.0001)
        self.state = 'Connected'

    async def file_recv(self, _id, filename, cache_modified, cache_hash, cache_time):
        self.state = 'Transfer'
        cache_path = os.path.normpath(os.path.join(os.getcwd(), f"cache/{filename}_{cache_time}"))
        os.makedirs(cache_path)
        with open(os.path.join(cache_path, filename), 'wb') as f:
            while True:
                buffer = await self.websocket.recv()
                if buffer == ':EOF':
                    break
                print("Recv chunk")
                f.write(buffer)
                await asyncio.sleep(0.0001)
        self.state = 'Connected' 
        if await get_hash(os.path.join(cache_path, filename)) != cache_hash:
            print(f"Download of {filename} from {self.hostname} was corrupt!..")
            shutil.rmtree(cache_path)
            return
        share = db.Shares.find_one({'_id': _id})
        share_path = None
        if share:        
            share_path = share['share_path']
            shutil.copy2(os.path.join(cache_path, filename), share_path)
        else:
            share_path = os.getcwd()
            shutil.copy2(os.path.join(cache_path, filename), share_path)
        mod_date = datetime.datetime.fromtimestamp(float(round(cache_modified)))
        mod_time = time.mktime(mod_date.timetuple())
        os.utime(share_path, (mod_time, mod_time))
        new_cache = {'cache_path': cache_path,
                     'cache_time': cache_time,
                     'cache_modified': mod_date.timestamp(),
                     'cache_hash': cache_hash}
        cache_list = []
        if share:
            cache_list = share['cache']
        cache_list.append(new_cache)
        if len(cache_list) > 2:
            old_cache = cache_list.pop(0)
            shutil.rmtree(old_cache['cache_path'])
        if share:
            db.Shares.update({'_id': _id}, {'$set': {'cache': cache_list}})
        else:
            db.Shares.insert({
                            '_id': _id,
                            'filename': filename, 
                            'share_path': share_path, 
                            'progress': 100,
                            'cache': [new_cache]
                            })
        shared_files = db.Shares.find()

    async def challenge_encode(self):
        SETTINGS = db.Settings.find_one({'_id': 'settings'})
        ip_sum1 = sum([int(i) for i in my_IP.split('.')])
        ip_sum2 = sum([int(i) for i in self.websocket.remote_address[0].split('.')])
        ip_sum = ip_sum1 + 2 * ip_sum2
        characters = string.ascii_lowercase + string.ascii_uppercase + string.digits
        challenge = ''.join([random.choice(characters) for _ in range(ip_sum)])
        mod = 3 - len(challenge) % 3
        if mod != 3:
            padding = '=' * mod
            challenge += padding
        salt = challenge[ip_sum1:ip_sum1+ip_sum2][::-1]
        timestamp = int(datetime.datetime.utcnow().timestamp())
        pass_hash = generate_password_hash(f"{salt}{SETTINGS['password']}{timestamp}")
        return challenge, pass_hash, timestamp

    async def challenge_decode(self, timestamp, challenge, pass_hash) -> bool:
        ip_sum1 = sum([int(i) for i in self.websocket.remote_address[0].split('.')])
        ip_sum2 = sum([int(i) for i in my_IP.split('.')])
        ip_sum = ip_sum1 + 2 * ip_sum2
        salt = challenge[ip_sum1:ip_sum1+ip_sum2][::-1]
        return check_password_hash(pass_hash, f"{salt}{SETTINGS['password']}{timestamp}")

    async def login(self):
        try:
            self.websocket = await websockets.connect(self.uri)
        except ConnectionRefusedError:
            print("Server connection refused")
            return
        except ConnectionError:
            print("Connection Error")
            return
        except OSError:
            print("OS Error")
            return
        except websockets.exceptions.ConnectionClosed:
            print("Connection closed")
        except:
            traceback.print_exc()
            return

        await self.send({'hostname': my_NAME})
        challenge = await self.recv()
        if 'challenge' not in challenge or 'hostname' not in challenge:
            return
        if len(challenge['challenge']) > 2048 or len(challenge['hostname']) > 1024:
            return

        self.hostname = challenge['hostname']        
        if not await self.challenge_decode(challenge['timestamp'], challenge['challenge'], challenge['key']):
            print(f"Invalid or Malicious Challenge from {self.hostname}")
            return

        encrypted_pass = rsa.encrypt(SETTINGS['password'].encode(), rsa.PublicKey.load_pkcs1(challenge['public_key'].encode()))
        password = {'password': base64.b64encode(encrypted_pass).decode()}

        await self.send(password)
        confirmation = await self.recv()
        confirmed = confirmation.get('Connection')
        if confirmed == 'authorized':
            self.state = 'Connected'
            print(f"Connected to {self.hostname}")
        else:
            print(f"Password mismatch on {self.hostname}")

    async def welcome(self) -> bool:
        greeting = await self.recv()
        if 'hostname' not in greeting:
            return False
        if len(greeting['hostname']) > 1024:
            return False
        self.hostname = greeting['hostname']
        self.uri = self.websocket.remote_address[0]
        challenge, pass_hash, timestamp = await self.challenge_encode()
        public_key, private_key  = rsa.newkeys(512)
        challenge = {'hostname': my_NAME,
                     'challenge': challenge,
                     'key': pass_hash,
                     'timestamp': timestamp,
                     'public_key': public_key.save_pkcs1().decode()}
        await self.send(challenge)
        password = await self.recv()
        if 'password' not in password:
            return False
        if len(password['password']) > 1024:
            return False

        decrypted_pass = rsa.decrypt(base64.b64decode(password['password'].encode()), private_key)
        if decrypted_pass.decode() == SETTINGS['password']:
            print("Cleared password check....\n")
            await self.send({'Connection': 'authorized'})
            self.state = 'Connected'
            asyncio.get_event_loop().create_task(self.listener())
            print(f"New connection from {self.hostname}")
            return True
        await self.send({'Connection': 'unauthorized'})
        return False

    async def listener(self):
        try:
            async for message in self.websocket:
                db = __client__['data']
                data = json.loads(message)
                op_type = data.get('op_type')
                if op_type == 'status':# and my_NAME == 'alchemist':
                    # print(f"{self.hostname} status:\n{data['connections']}\n{data['shares']}")
                    self.shares = data['shares']
                    self.peers = data['connections']
                    for share in self.shares:
                        wanted = db.Shares.find_one({'_id': share['_id']})
                        if not wanted or(wanted['cache'][-1]['cache_hash'] != share['cache_hash'] 
                            and wanted['cache'][-1]['cache_time'] < share['cache_time']):
                                await self.send({'op_type': 'request',
                                                    '_id': share['_id'],
                                                    'filename': share['filename']})
                        
                if op_type == 'request':
                    shared_files = db.Shares.find()
                    print(f"{self.hostname} request:\n{data['filename']}")
                    for share in shared_files:
                        if share['_id'] == data['_id']:
                            filename = share['filename']
                            cache_modified = share['cache'][-1]['cache_modified']
                            cache_path = share['cache'][-1]['cache_path']
                            cache_hash = share['cache'][-1]['cache_hash']
                            cache_time = share['cache'][-1]['cache_time']
                            await self.send({'op_type': 'sending',
                                             '_id': share['_id'],
                                             'filename': filename,
                                             'cache_hash': cache_hash,
                                             'cache_modified': cache_modified,
                                             'cache_time': cache_time})

                            await self.file_send(os.path.join(cache_path, filename))
                if op_type == 'sending':
                    print(f"{self.hostname} confirms:\n{data['filename']}")
                    # Take all the details for the file and receive it.
                    await self.file_recv(data['_id'], data['filename'], data['cache_modified'], data['cache_hash'], data['cache_time'])
                    print("Download complete...")
        except websockets.exceptions.ConnectionClosed:
            print(f"Connection Closed from {self.hostname}")
            await unregister(self)
        except:
            traceback.print_exc()
            await unregister(self)
        # finally:
            
    async def close(self):
        state = 'Disconnected'
        try:
            await self.websocket.close()
        except:
            traceback.print_exc()
    

class ServerHandler(ConnectionHandler):
    def __init__(self, websocket):
        self.websocket = websocket


class ClientHandler(ConnectionHandler):
    def __init__(self, uri):
        self.uri = uri

async def port_scanner():
    if not(my_IP[:3] == '192' or my_IP[:3] == '10.' or my_IP[:3] == '172'):
        print("This is not a private network...\nSHUTTING DOWN!!")
        exit()
    ip_range = '.'.join(my_IP.split('.')[:3])
    for i in range(5, 10):
        target_ip = f"{ip_range}.{i}"
        print(target_ip)
        uri = f"ws://{target_ip}:1111"
        connection = ClientHandler(uri)
        await connection.login()
        if connection.state == 'Connected':
            CONNECTIONS.add(connection)
            asyncio.get_event_loop().create_task(connection.listener())
        await asyncio.sleep(0.0001)
        
# Second parameter is path which we dont need
# Server / Receiver side happenings
async def register_client(websocket, _):
    connection = ServerHandler(websocket)
    done = False
    while True:
        if not done:
            if await connection.welcome():
                CONNECTIONS.add(connection)
                done = True
        await asyncio.sleep(0.0001)

async def unregister(connection):
    await connection.close()
    try:
        CONNECTIONS.remove(connection)
    except:
        traceback.print_exc()

async def status_update():
    while True:
        print(f"Updating Status...{len(CONNECTIONS)}")
        db = __client__['data']
        shared_files = db.Shares.find()
        connection_list = []
        share_list = []
        for CONNECTION in CONNECTIONS:
            connection_list.append({'hostname': CONNECTION.hostname, 'uri': CONNECTION.uri})
        # print("Share list from status update")
        for share in shared_files:
            # print(share)
            share_list.append({'_id': share['_id'],
                               'filename': share['filename'],
                               'cache_modified': share['cache'][-1]['cache_modified'],
                               'cache_hash': share['cache'][-1]['cache_hash'],
                               'cache_time': share['cache'][-1]['cache_time']})

        for CONNECTION in CONNECTIONS:
            if CONNECTION.state == 'Connected':
                await CONNECTION.send({'op_type': 'status', 
                                       'hostname': my_NAME, 
                                       'connections': connection_list,
                                       'shares': share_list})
        await asyncio.sleep(10)

start_server = websockets.serve(register_client, my_IP, 1111)
if __name__ == '__main__':
    asyncio.get_event_loop().run_until_complete(start_server)
    asyncio.get_event_loop().create_task(status_update())
    asyncio.get_event_loop().run_forever()

