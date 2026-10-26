
import argparse
import os
import socket
import string
import sys
import threading
import time
from datetime import datetime

from chat_protocol import (CRYPTO_AVAILABLE, IdleTimeout, PROTOCOL_VERSION,
                           ProtocolError, human_size, local_address,
                           new_key_pair, recv_frame, send_frame, shared_channel)

SERVER_NAME = "SERVER"
VALID_NAME_CHARS = set(string.ascii_letters + string.digits + "_-.")
RESERVED_NAMES = {"server", "all", "everyone", "global", "none"}
SOCKET_TIMEOUT = 60.0  # wait this long before we check a client
DROP_AFTER = 300.0  # silent this long means the client is gone

#class
#store that user's specific socket, username, and status.
class ClientSession:
    def __init__(self, sock, address):
        self.sock = sock
        self.address = address
        self.user_name = None
        self.channel = None  # the encryption box for this client
        self.send_lock = threading.Lock()  # only one thread may write at a time
        self.connected_at = time.time()
        self.last_seen = time.time()
        self.is_alive = True

    @property              #it makes class functions in read only attribute so we can use them as variable
    def label(self):
       
        return "%s@%s:%d" % (self.user_name or "?", self.address[0], self.address[1])

    #to send data safely to the client, if sending fails it maks client dead
    
    def send(self, header, payload=b""):
       
        if not self.is_alive:
            return False
        try:
            send_frame(self.sock, self.send_lock, header, payload, self.channel)
            return True
        except (OSError, ProtocolError):
            self.is_alive = False
            return False

     # send a notification message to client
    def notice(self, text, level="info"):
        
        return self.send({"type": "NOTICE", "level": level, "text": text,
                          "at": time.time()})

    # shutsdown the socket connection
    def close(self):
        
        self.is_alive = False
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


# The main server class. It keeps all the lists: who is online, which rooms
# exist, which files are being sent and which calls are running.

class ChatServer:
    def __init__(self, host="0.0.0.0", port=5050, encrypt=True):
        self.host = host
        self.port = port
        self.encrypt = encrypt and CRYPTO_AVAILABLE
        self.state_lock = threading.RLock()  # guards all the lists below
        self.print_lock = threading.Lock()  # stops log lines mixing together
        self.clients = {}  # name -> ClientSession
        self.rooms = {"general": set()}  # room name -> set of names inside it
        self.transfers = {}  # file id -> where that file is going
        self.peer_calls = {}  # name -> the name they are calling
        self.invites = {}  # name -> (who is calling, voice/video)
        self.conference = None  # the group call, when one is running
        self.listener = None
        self.is_running = False
        self.started_at = time.time()
        self.message_count = 0
        self.bytes_relayed = 0

    #FUNCTIONS USED IN STARTUP
    # Print one tidy line with the time in front.
    def log(self, text, tag="SERVER"):
        line = "%s  %-8s %s" % (datetime.now().strftime("%H:%M:%S"), tag, text)
        with self.print_lock:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()

    # The exact command the other laptops should type.
    def connect_hint(self):
        suffix = "" if self.port == 5050 else " --port %d" % self.port
        return "python client.py --server %s%s" % (local_address(), suffix)

    # The box of text printed once when the server starts.
    def print_banner(self):
        bar = "=" * 72
        lines = [bar,
                 "  LAN CHAT SERVER                      protocol %s"
                 % PROTOCOL_VERSION,
                 bar,
                 "  listening on   : %s:%d" % (self.host, self.port),
                 "  encryption     : %s" % ("ON (X25519 + AES-GCM)"
                                            if self.encrypt else "OFF"),
                 "",
                 "  on every other laptop on this Wi-Fi run:",
                 "      %s" % self.connect_hint(),
                 "  on this same laptop:  python client.py --server 127.0.0.1",
                 "",
                 "  operator commands : /help",
                 bar]
        with self.print_lock:
            sys.stdout.write("\n".join(lines) + "\n")
            sys.stdout.flush()

    # How long the server has been running.
    def uptime(self):
        seconds = int(time.time() - self.started_at)
        return "%dh %02dm %02ds" % (seconds // 3600, (seconds % 3600) // 60,
                                    seconds % 60)

    # Open the port and wait for clients, forever, until Ctrl-C.
    def start(self):
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)   #it is a tcp socket with port 5050
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.listener.bind((self.host, self.port))
        except OSError as error:
            
            self.log("cannot bind %s:%d - %s" % (self.host, self.port, error),
                     "FATAL")
            return
        self.listener.listen(64)
        self.listener.settimeout(1.0)  # short wait so Ctrl-C still works
        self.is_running = True
        self.print_banner()
        OperatorConsole(self).start()  # start reading typed commands
        try:
            while self.is_running:
                try:
                    sock, address = self.listener.accept()            #to accept new clients automatically that are connecting 
                except socket.timeout:
                    continue  # if nobody came itg  will run again 
                except OSError:
                    break
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                session = ClientSession(sock, address)
                # Give every client its own thread, so they do not wait for
                # each other. This is the part the assignment asks for.
                threading.Thread(target=self.serve_client, args=(session,),
                                 daemon=True).start()
                self.log("connection from %s:%d" % address, "NET")
        except KeyboardInterrupt:
            self.log("keyboard interrupt, shutting down")
        finally:
            self.shutdown()

    # Tell everybody we are closing, then close every socket.
    def shutdown(self):
        if not self.is_running:
            return
        self.is_running = False
        if self.conference:
            self.end_conference("the server is shutting down")
        for session in self.all_sessions():
            session.send({"type": "SHUTDOWN", "text": "the server is shutting down"})
            session.close()
        try:
            self.listener.close()
        except (OSError, AttributeError):
            pass
        self.log("server stopped (uptime %s)" % self.uptime())

    #THREAD
    # This runs in its own thread, one copy per client.
    def serve_client(self, session):
        
        try:
            self.handshake(session)  # agree the key
            if not self.login(session):  # get the name
                return
            session.sock.settimeout(SOCKET_TIMEOUT)
            # Now just keep reading messages and passing them on.
            while self.is_running and session.is_alive:
                try:
                    header, payload = recv_frame(session.sock, session.channel)
                except IdleTimeout:
                    # Quiet is normal. But if it is quiet for very long the
                    # laptop is probably switched off, so we drop it.
                    if time.time() - session.last_seen > DROP_AFTER:
                        raise ConnectionError("client stopped responding")
                    continue
                session.last_seen = time.time()
                self.route(session, header, payload)
        except (ConnectionError, ProtocolError, OSError, EOFError) as error:
            self.log("%s dropped: %s" % (session.label, error), "NET")
        except Exception as error:  # one bad client never kills the server
            self.log("unexpected error for %s: %r" % (session.label, error), "ERROR")
        finally:
            self.cleanup(session)  # always tidy up afterwards

    def handshake(self, session):
        
        # We send our public key, the client sends its public key back, and
        # both sides work out the same secret key from that.
        private_key, public_hex = (new_key_pair() if self.encrypt else (None, None))
        send_frame(session.sock, session.send_lock,
                   {"type": "HELLO", "version": PROTOCOL_VERSION,
                    "encryption": self.encrypt, "public_key": public_hex})
        session.sock.settimeout(30.0)
        header, _ = recv_frame(session.sock)  # this one is not locked yet
        if header.get("type") != "KEX":
            raise ProtocolError("expected a key exchange frame")
        peer_public = header.get("public_key")
        if self.encrypt and peer_public:
            session.channel = shared_channel(private_key, peer_public)
            self.log("secure channel with %s:%d" % session.address, "CRYPTO")

    def login(self, session):
        
        session.sock.settimeout(60.0)
        while self.is_running:
            header, _ = recv_frame(session.sock, session.channel)
            if header.get("type") != "LOGIN":
                session.notice("you must log in first", "error")
                continue
            name = str(header.get("user_name", "")).strip()
            # Check the name and save it, all inside the lock, so two people
            # cannot grab the same name at the same moment.
            with self.state_lock:
                problem = self.check_name(name)
                if problem is None:
                    session.user_name = name
                    self.clients[name] = session
            if problem:
                session.send({"type": "LOGIN_RESULT", "ok": False, "reason": problem})
                continue
            session.send({"type": "LOGIN_RESULT", "ok": True, "user_name": name,
                          "encryption": bool(session.channel),
                          "text": "welcome to the LAN chat server"})
            self.log("%s logged in from %s" % (name, session.address[0]), "LOGIN")
            # Tell everyone somebody joined, and send the new online list.
            self.announce_system("%s joined the chat" % name)
            self.push_state()
            self.show_roster()
            return True
        return False

     # t
    def check_name(self, name):
       
        if not 1 <= len(name) <= 20:
            return "name must be 1 to 20 characters"
        if not set(name) <= VALID_NAME_CHARS:
            return "use letters, digits, dot, dash or underscore only"
        if name.lower() in RESERVED_NAMES:
            return "'%s' is a reserved name" % name
        if name.lower() in {taken.lower() for taken in self.clients}:
            return "'%s' is already online" % name
        return None

    # Runs when a client leaves. We must remove it from every list, or the
    # server will keep trying to send to a dead socket.
    def cleanup(self, session):
        name = session.user_name
        session.close()
        if not name:
            return  # it never logged in, nothing to clean
        with self.state_lock:
            if self.clients.get(name) is session:
                del self.clients[name]
            for members in self.rooms.values():
                members.discard(name)  # take it out of every room
            peer = self.peer_calls.pop(name, None)
            if peer:
                self.peer_calls.pop(peer, None)
            self.invites.pop(name, None)
            # Cancel any call this person was making to somebody else.
            for callee, (caller, _media) in list(self.invites.items()):
                if caller == name:
                    del self.invites[callee]
            # Throw away any file it was in the middle of sending.
            for transfer_id in [key for key, info in self.transfers.items()
                                if info["sender"] == name]:
                del self.transfers[transfer_id]
            peer_session = self.clients.get(peer) if peer else None
            in_conference = bool(self.conference) and name in self.conference["members"]
            if self.conference:
                self.conference["members"].discard(name)
                self.conference["invited"].discard(name)
        # Sending happens after the lock is released.
        if peer_session:
            peer_session.send({"type": "CALL_END", "peer": name,
                               "reason": "%s disconnected" % name})
        if in_conference:
            self.push_conference_members()
        self.close_conference_if_empty()
        self.announce_system("%s left the chat" % name)
        self.push_state()
        self.log("%s disconnected (%d online)" % (name, len(self.clients)), "LOGOUT")
        self.show_roster()

    def show_roster(self):
        """The live 'who is connected' line on the operator's screen."""
        with self.state_lock:
            if not self.clients:
                self.log("active clients: none", "STATUS")
                return
            # Build a line like:  ali(#project), anas(lobby+call:ali)
            parts = []
            for name in sorted(self.clients):
                rooms = [room for room, members in self.rooms.items()
                         if name in members]
                where = ",".join("#" + room for room in sorted(rooms)) or "lobby"
                if name in self.peer_calls:
                    where += "+call:" + self.peer_calls[name]
                elif self.conference and name in self.conference["members"]:
                    where += "+conference"
                parts.append("%s(%s)" % (name, where))
        self.log("active clients [%d]: %s" % (len(parts), ", ".join(parts)), "STATUS")

    # routing
    # Look at the "type" of the message and call the matching function.
    # This is the middle of the whole server.

    # server recievers messages from client through recive_frame function then route it to other client using function self route
    def route(self, session, header, payload):
        handlers = {
            "PING": self.on_ping,
            "CHAT": self.on_chat,
            "COMMAND": self.on_command,
            "FILE_BEGIN": self.on_file_begin,
            "FILE_CHUNK": self.on_file_chunk,
            "FILE_END": self.on_file_end,
            "CALL_INVITE": self.on_call_invite,
            "CALL_ACCEPT": self.on_call_accept,
            "CALL_REJECT": self.on_call_reject,
            "CALL_END": self.on_call_end,
            "CALL_MEDIA": self.on_call_media,
        }
        handler = handlers.get(header.get("type"))
        if handler is None:
            session.notice("unknown frame type: %r" % header.get("type"), "error")
        else:
            handler(session, header, payload)

    # FEATURE HANDLERS
    # The client asks "are you there?" and we answer.
    def on_ping(self, session, header, _payload):
        session.send({"type": "PONG", "token": header.get("token")})

    # text chat
    def on_chat(self, session, header, _payload):
        """Four destinations: the server, one user, one room, or everybody."""
        text = str(header.get("text", "")).strip()
        if not text:
            return  # ignore empty messages
        scope = header.get("scope", "global")
        self.message_count += 1
        stamp = time.time()

        if scope == "server":  # the client is writing to us
            self.log("%s: %s" % (session.user_name, text), "INBOX")
            # Send it back so the client can see its own message too.
            session.send({"type": "CHAT", "scope": "server", "sender":
                          session.user_name, "text": text, "at": stamp})

        elif scope == "private":  # message for one person only
            target = str(header.get("target", ""))
            destination = self.find(target)
            if destination is None:
                session.notice("'%s' is not online" % target, "error")
                return
            message = {"type": "CHAT", "scope": "private",
                       "sender": session.user_name, "target": destination.user_name,
                       "text": text, "at": stamp}
            destination.send(message)
            session.send(message)  # one copy each, so both see the same
            self.log("%s -> %s: %s" % (session.user_name, destination.user_name,
                                       text), "PRIVATE")

        elif scope == "room":  # message for one room
            room = str(header.get("room", "")).lower()
            if not self.is_member(session.user_name, room):
                session.notice("join #%s before writing in it" % room, "error")
                return
            self.send_room(room, {"type": "CHAT", "scope": "room", "room": room,
                                  "sender": session.user_name, "text": text,
                                  "at": stamp})
            self.log("[#%s] %s: %s" % (room, session.user_name, text), "ROOM")

        else:  # message for everybody
            self.send_all({"type": "CHAT", "scope": "global",
                           "sender": session.user_name, "text": text, "at": stamp})
            self.log("[all] %s: %s" % (session.user_name, text), "CHAT")

    def announce(self, text):
        """/say: an announcement from the operator to every client."""
        self.message_count += 1
        self.send_all({"type": "CHAT", "scope": "global", "sender": SERVER_NAME,
                       "text": text, "at": time.time()})
        self.log("to everyone: %s" % text, "ANNOUNCE")

    def message_user(self, name, text):
        """/msg: a private message from the operator to one client."""
        destination = self.find(name)
        if destination is None:
            self.log("no such user: %s" % name, "ADMIN")
            return False
        self.message_count += 1
        destination.send({"type": "CHAT", "scope": "private", "sender": SERVER_NAME,
                          "target": destination.user_name, "text": text,
                          "at": time.time()})
        self.log("to %s: %s" % (destination.user_name, text), "ANNOUNCE")
        return True

    # Small "x joined" / "x left" line shown to everybody.
    def announce_system(self, text):
        self.send_all({"type": "SYSTEM", "text": text, "at": time.time()})

    # rooms
    # Commands the client sends us: make a room, join one, leave one, ask status.
    def on_command(self, session, header, _payload):
        command = str(header.get("command", "")).lower()
        argument = str(header.get("argument", "")).strip().lower()

        if command in ("create_room", "join_room"):
            # Same code for both: if the room is new we make it, then join.
            if not 1 <= len(argument) <= 24 or not set(argument) <= VALID_NAME_CHARS:
                session.notice("room name: 1 to 24 letters, digits, dot, dash, "
                               "underscore", "error")
                return
            with self.state_lock:
                is_new = argument not in self.rooms
                self.rooms.setdefault(argument, set()).add(session.user_name)
            verb = "created and joined" if is_new else "joined"
            self.send_room(argument, {"type": "SYSTEM", "room": argument,
                                      "text": "%s %s #%s" % (session.user_name,
                                                             verb, argument),
                                      "at": time.time()})
            self.log("%s %s #%s" % (session.user_name, verb, argument), "ROOM")
            self.push_state()
            self.show_roster()

        elif command == "leave_room":
            with self.state_lock:
                was_member = session.user_name in self.rooms.get(argument, set())
                if was_member:
                    self.rooms[argument].discard(session.user_name)
                    # An empty room is deleted, but we always keep general.
                    if not self.rooms[argument] and argument != "general":
                        del self.rooms[argument]
            if not was_member:
                session.notice("you are not in #%s" % argument, "error")
                return
            self.send_room(argument, {"type": "SYSTEM", "room": argument,
                                      "text": "%s left #%s" % (session.user_name,
                                                               argument),
                                      "at": time.time()})
            session.send({"type": "ROOM_LEFT", "room": argument})
            self.log("%s left #%s" % (session.user_name, argument), "ROOM")
            self.push_state()

        elif command == "status":
            # Numbers about the server, for whoever asked.
            with self.state_lock:
                session.send({"type": "STATUS", "uptime": self.uptime(),
                              "online": len(self.clients),
                              "rooms": len(self.rooms),
                              "messages": self.message_count,
                              "relayed": human_size(self.bytes_relayed),
                              "encryption": bool(session.channel)})
        else:
            session.notice("unknown command '%s'" % command, "error")

    # Is this person inside this room?
    def is_member(self, name, room):
        with self.state_lock:
            return name in self.rooms.get(room, set())

    def push_state(self):
        """One snapshot of users and rooms; every client renders its own view."""
        # We send one list to everybody instead of many small updates.
        # Each client then decides what to show in its sidebar.
        with self.state_lock:
            snapshot = {"type": "STATE",
                        "users": sorted(self.clients),
                        "rooms": {room: sorted(members)
                                  for room, members in sorted(self.rooms.items())},
                        "at": time.time()}
        self.send_all(snapshot)

    # file transfer
    #  file is sent in three steps:
    #   FILE_BEGIN  -> we check who it is for and give back an id
    #   FILE_CHUNK  -> many times, 64 KB each, we just pass them along
    #   FILE_END    -> the checksum, so the receiver can check nothing was lost
    
    def on_file_begin(self, session, header, _payload):
        file_name = os.path.basename(str(header.get("file_name", "file.bin")))
        size = int(header.get("size", 0))
        kind = header.get("kind", "file")  # file image video voice
        targets, label = self.resolve_targets(session, str(header.get("to", "")))

        # Work out whether we can deliver it at all.
        if targets is None:
            reason = "no such user or room: '%s'" % header.get("to")
        elif not targets:
            reason = "nobody is there to receive it (%s)" % label
        else:
            reason = None
        if reason:
            session.send({"type": "FILE_REJECTED", "reference": header.get("reference"),
                          "file_name": file_name, "reason": reason})
            return

        # A unique id for this one file, so chunks cannot get mixed up.
        transfer_id = "%s-%d-%s" % (session.user_name, int(time.time() * 1000),
                                    os.urandom(3).hex())
        with self.state_lock:
            self.transfers[transfer_id] = {"sender": session.user_name,
                                           "targets": targets, "label": label,
                                           "file_name": file_name, "size": size,
                                           "received": 0, "started": time.time()}
        # Warn the receivers, then tell the sender to start sending.
        self.send_to(targets, {"type": "FILE_BEGIN", "transfer_id": transfer_id,
                               "sender": session.user_name, "to": label,
                               "file_name": file_name, "size": size, "kind": kind,
                               "at": time.time()})
        session.send({"type": "FILE_ACCEPTED", "reference": header.get("reference"),
                      "transfer_id": transfer_id, "file_name": file_name,
                      "to": label, "size": size})
        self.log("%s -> %s: '%s' (%s, %s) starting"
                 % (session.user_name, label, file_name, kind, human_size(size)),
                 "FILE")

    # One 64 KB piece arrives: count it and pass it straight on.
    def on_file_chunk(self, session, header, payload):
        with self.state_lock:
            info = self.transfers.get(header.get("transfer_id"))
            if not info or info["sender"] != session.user_name:
                session.notice("unknown transfer id", "error")
                return
            info["received"] += len(payload)
            self.bytes_relayed += len(payload)
            targets = list(info["targets"])  # copy inside the lock
        self.send_to(targets, {"type": "FILE_CHUNK",
                               "transfer_id": header.get("transfer_id"),
                               "sequence": header.get("sequence", 0)}, payload)

    # The file is finished: pass on the checksum and forget the transfer.
    def on_file_end(self, session, header, _payload):
        with self.state_lock:
            info = self.transfers.pop(header.get("transfer_id"), None)
        if not info or info["sender"] != session.user_name:
            session.notice("unknown transfer id", "error")
            return
        seconds = max(time.time() - info["started"], 1e-6)  # never divide by zero
        self.send_to(info["targets"], {"type": "FILE_END",
                                       "transfer_id": header.get("transfer_id"),
                                       "sender": session.user_name,
                                       "file_name": info["file_name"],
                                       "size": info["received"],
                                       "sha256": header.get("sha256"),
                                       "at": time.time()})
        session.send({"type": "FILE_SENT", "file_name": info["file_name"],
                      "size": info["received"], "to": info["label"]})
        self.log("'%s' delivered to %s: %s in %.1fs (%s/s)"
                 % (info["file_name"], info["label"], human_size(info["received"]),
                    seconds, human_size(info["received"] / seconds)), "FILE")

    def resolve_targets(self, session, target):
        
        with self.state_lock:
            if not target or target.lower() in ("all", "everyone"):
                return [name for name in self.clients
                        if name != session.user_name], "everyone"
            if target.startswith("#"):  # a room, written like #project
                room = target[1:].lower()
                if room in self.rooms:
                    return [name for name in self.rooms[room]
                            if name != session.user_name], "#" + room
                return None, None
            destination = self.find(target, already_locked=True)
            if destination is not None:
                return [destination.user_name], destination.user_name
        return None, None

    # voice and video
    # Somebody is busy if they are in a call, being rung, or in the conference.
    def is_busy(self, name):
        return (name in self.peer_calls or name in self.invites
                or (self.conference and name in self.conference["members"]))

    # One client wants to call another one.
    def on_call_invite(self, session, header, _payload):
        target = str(header.get("to", "")).strip()
        media = "video" if header.get("media") == "video" else "voice"
        with self.state_lock:
            destination = self.find(target, already_locked=True)
            # All the reasons a call cannot start.
            if destination is None:
                session.notice("'%s' is not online" % target, "error")
                return
            if destination.user_name == session.user_name:
                session.notice("you cannot call yourself", "error")
                return
            if self.is_busy(session.user_name):
                session.notice("you are already in a call", "error")
                return
            if self.is_busy(destination.user_name):
                session.notice("%s is busy on another call"
                               % destination.user_name, "error")
                return
            # Remember who is ringing whom, until they answer.
            self.invites[destination.user_name] = (session.user_name, media)
        destination.send({"type": "CALL_INVITE", "sender": session.user_name,
                          "media": media})
        session.notice("ringing %s (%s call)" % (destination.user_name, media))
        self.log("%s is calling %s (%s)" % (session.user_name,
                                            destination.user_name, media), "CALL")

    def start_conference(self, media):
        """/callall: ring every free client and bridge them to each other."""
        
        
        if self.conference:
            self.log("a conference is already running, /endcall first", "CALL")
            return False
        with self.state_lock:
            invitees = [name for name in self.clients if not self.is_busy(name)]
            if not invitees:
                self.log("nobody is available to call", "CALL")
                return False
            self.conference = {"media": media, "members": set(),
                               "invited": set(invitees), "started": time.time()}
            for name in invitees:
                self.invites[name] = (SERVER_NAME, media)
        self.send_to(invitees, {"type": "CALL_INVITE", "sender": SERVER_NAME,
                                "media": media, "conference": True})
        self.log("ringing everybody for a %s conference: %s"
                 % (media, ", ".join(invitees)), "CALL")
        return True

    # Tell everybody in the conference who else is in it now.
    def push_conference_members(self):
        if not self.conference:
            return
        members = sorted(self.conference["members"])
        self.send_to(members, {"type": "CALL_MEMBERS", "members": members,
                               "media": self.conference["media"]})

    # If nobody is left in the group call, close it.
    def close_conference_if_empty(self):
        if self.conference and not (self.conference["members"]
                                    or self.conference["invited"]):
            self.end_conference("everybody has left the conference")

    def end_conference(self, reason="the operator ended the conference"):
        if not self.conference:
            self.log("there is no conference running", "CALL")
            return
        with self.state_lock:
            conference = self.conference
            # Everybody who joined, plus everybody still being rung.
            everyone = sorted(conference["members"] | conference["invited"])
            for name in everyone:
                self.invites.pop(name, None)
            self.conference = None
        seconds = int(time.time() - conference["started"])
        self.send_to(everyone, {"type": "CALL_END", "peer": "conference",
                                "reason": reason})
        self.log("%s conference ended after %02d:%02d"
                 % (conference["media"], seconds // 60, seconds % 60), "CALL")
        self.show_roster()

    # Somebody pressed yes on a ringing call.
    def on_call_accept(self, session, _header, _payload):
        with self.state_lock:
            invite = self.invites.pop(session.user_name, None)
        if not invite:
            session.notice("there is no incoming call to accept", "error")
            return
        caller, media = invite

        if caller == SERVER_NAME:  # joining the conference
            if not self.conference:
                session.notice("the conference has already ended", "error")
                return
            with self.state_lock:
                self.conference["members"].add(session.user_name)
                self.conference["invited"].discard(session.user_name)
                members = sorted(self.conference["members"])
            session.send({"type": "CALL_START", "peer": "conference",
                          "media": media, "conference": True, "members": members})
            self.push_conference_members()
            self.log("%s joined the conference (%s)"
                     % (session.user_name, ", ".join(members)), "CALL")
            self.show_roster()
            return

        with self.state_lock:  # a normal one to one call
            peer = self.clients.get(caller)
            if peer is None:
                session.notice("%s is no longer online" % caller, "error")
                return
            # Write the pair both ways, so either side can be looked up.
            self.peer_calls[session.user_name] = caller
            self.peer_calls[caller] = session.user_name
        session.send({"type": "CALL_START", "peer": caller, "media": media})
        peer.send({"type": "CALL_START", "peer": session.user_name, "media": media})
        self.log("connected: %s <-> %s (%s)" % (caller, session.user_name, media),
                 "CALL")
        self.show_roster()

    # Somebody pressed no.
    def on_call_reject(self, session, _header, _payload):
        with self.state_lock:
            invite = self.invites.pop(session.user_name, None)
        if not invite:
            return
        caller = invite[0]
        if caller == SERVER_NAME:  # they said no to the group call
            with self.state_lock:
                if self.conference:
                    self.conference["invited"].discard(session.user_name)
            self.log("%s declined the conference" % session.user_name, "CALL")
            self.close_conference_if_empty()
            return
        peer = self.find(caller)
        if peer:
            peer.send({"type": "CALL_END", "peer": session.user_name,
                       "reason": "%s declined the call" % session.user_name})
        self.log("%s declined a call from %s" % (session.user_name, caller), "CALL")

    # Somebody hung up. They might be in the group call or in a normal call.
    def on_call_end(self, session, _header, _payload):
        name = session.user_name
        if self.conference and name in self.conference["members"]:
            with self.state_lock:
                self.conference["members"].discard(name)
            session.send({"type": "CALL_END", "peer": "conference",
                          "reason": "you left the conference"})
            self.push_conference_members()
            self.log("%s left the conference" % name, "CALL")
            self.close_conference_if_empty()
            self.show_roster()
            return
        with self.state_lock:
            peer_name = self.peer_calls.pop(name, None)
            if peer_name:
                self.peer_calls.pop(peer_name, None)
            self.invites.pop(name, None)
            # Maybe they hung up while still ringing somebody.
            for callee, (caller, _media) in list(self.invites.items()):
                if caller == name:
                    del self.invites[callee]
                    peer_name = peer_name or callee
            peer = self.clients.get(peer_name) if peer_name else None
        if peer:
            peer.send({"type": "CALL_END", "peer": name, "reason": "%s hung up" % name})
        session.send({"type": "CALL_END", "peer": peer_name, "reason": "call ended"})
        if peer_name:
            self.log("ended: %s <-> %s" % (name, peer_name), "CALL")
            self.show_roster()

    def on_call_media(self, session, header, payload):
       
               
        # this runs about 20 times a second for every person in a call.
        name = session.user_name
        outgoing = {"type": "CALL_MEDIA", "sender": name,
                    "media": header.get("media", "audio"),
                    "sequence": header.get("sequence", 0)}
        with self.state_lock:
            if self.conference and name in self.conference["members"]:
                # In a group call everyone else hears and sees this person.
                listeners = [member for member in self.conference["members"]
                             if member != name]
            else:
                # In a normal call there is only one other person.
                peer_name = self.peer_calls.get(name)
                listeners = [peer_name] if peer_name else []
            if not listeners:
                return  # the call is over, drop this frame
            self.bytes_relayed += len(payload)
        self.send_to(listeners, outgoing, payload)

    # The four helpers below all follow the same safe pattern:
    # copy the list of people while holding the lock, then send outside it.
    def find(self, name, already_locked=False):
        """Find a session by user name, ignoring upper and lower case."""

        def look():
            if name in self.clients:
                return self.clients[name]
            lowered = name.lower()
            for user_name, session in self.clients.items():
                if user_name.lower() == lowered:
                    return session
            return None
        # already_locked is True when the caller is inside the lock already.
        if already_locked:
            return look()
        with self.state_lock:
            return look()

    def all_sessions(self):
        with self.state_lock:
            return list(self.clients.values())

    # Send to everybody online.
    def send_all(self, header, payload=b""):
        for session in self.all_sessions():
            session.send(header, payload)

    # Send to the people in one room.
    def send_room(self, room, header, payload=b""):
        with self.state_lock:
            targets = [self.clients[name] for name in self.rooms.get(room, set())
                       if name in self.clients]
        for session in targets:
            session.send(header, payload)

    # Send to a list of names we were given.
    def send_to(self, user_names, header, payload=b""):
        with self.state_lock:
            targets = [self.clients[name] for name in user_names
                       if name in self.clients]
        for session in targets:
            session.send(header, payload)


# Reads the commands typed on this laptop, in its own thread, so typing never
# stops the clients from chatting.
class OperatorConsole:
    HELP = [("/say <message>", "announcement to every connected client"),
            ("/msg <user> <message>", "private message to one client"),
            ("/callall [video]", "ring everybody for a voice or video conference"),
            ("/endcall", "end the conference"),
            ("/users", "the active client list"),
            ("/rooms", "rooms and their members"),
            ("/calls", "every call in progress"),
            ("/status", "uptime, counters, data relayed"),
            ("/ip", "the command the other laptops should run"),
            ("/kick <user>", "disconnect one client"),
            ("/help", "this list"),
            ("/quit", "stop the server")]

    def __init__(self, server):
        self.server = server

    def start(self):
        threading.Thread(target=self.read_loop, daemon=True).start()

    # Keep reading whatever is typed, line by line.
    def read_loop(self):
        while self.server.is_running:
            try:
                line = sys.stdin.readline()
            except (OSError, ValueError):
                return
            if not line:  # input closed
                return
            line = line.strip()
            if not line:
                continue  # just the enter key
            if not line.startswith("/"):
                self.server.log("type /say <message> to announce, or /help", "ADMIN")
                continue
            # Split "/msg ali hello" into "/msg" and "ali hello".
            command, _, argument = line.partition(" ")
            self.run(command.lower(), argument.strip())

    # One long if/elif: find the command that was typed and do it.
    def run(self, command, argument):
        server = self.server
        if command in ("/say", "/announce"):
            if argument:
                server.announce(argument)
            else:
                server.log("usage: /say <message>", "ADMIN")
        elif command in ("/msg", "/pm"):
            # First word is the name, the rest is the message.
            name, _, text = argument.partition(" ")
            if name and text.strip():
                server.message_user(name, text.strip())
            else:
                server.log("usage: /msg <user> <message>", "ADMIN")
        elif command in ("/callall", "/conference"):
            # Anything starting with v means video, otherwise voice.
            server.start_conference("video" if argument.lower().startswith("v")
                                    else "voice")
        elif command in ("/endcall", "/hangup"):
            server.end_conference()
        elif command in ("/users", "/who"):
            server.show_roster()
        elif command == "/rooms":
            with server.state_lock:
                rooms = {room: sorted(members)
                         for room, members in server.rooms.items()}
            for room, members in sorted(rooms.items()):
                server.log("#%-16s %s" % (room, ", ".join(members) or "(empty)"),
                           "STATUS")
        elif command == "/calls":
            with server.state_lock:
                # The pair list holds each call twice, so we sort and de-duplicate.
                pairs = sorted({tuple(sorted(pair))
                                for pair in server.peer_calls.items()})
                conference = server.conference
            for first, second in pairs:
                server.log("%s <-> %s" % (first, second), "CALL")
            if conference:
                server.log("%s conference: %s" % (conference["media"],
                           ", ".join(sorted(conference["members"])) or "nobody yet"),
                           "CALL")
            if not pairs and not conference:
                server.log("no calls in progress", "CALL")
        elif command == "/status":
            with server.state_lock:
                server.log("uptime %s | %d online | %d rooms | %d messages | %s "
                           "relayed" % (server.uptime(), len(server.clients),
                                        len(server.rooms), server.message_count,
                                        human_size(server.bytes_relayed)), "STATUS")
        elif command == "/ip":
            server.log("other laptops run:  %s" % server.connect_hint(), "STATUS")
        elif command == "/kick":
            # Tell the person why, then close their socket.
            victim = server.find(argument)
            if victim:
                victim.send({"type": "SHUTDOWN",
                             "text": "you were disconnected by the operator"})
                victim.close()
                server.log("kicked %s" % argument, "ADMIN")
            else:
                server.log("no such user: %s" % argument, "ADMIN")
        elif command == "/help":
            with server.print_lock:
                sys.stdout.write("\n  SERVER OPERATOR COMMANDS\n")
                for usage, meaning in self.HELP:
                    sys.stdout.write("    %-24s %s\n" % (usage, meaning))
                sys.stdout.write("\n")
                sys.stdout.flush()
        elif command in ("/quit", "/stop"):
            server.shutdown()
            os._exit(0)  # stop at once, do not wait for threads
        else:
            server.log("unknown command '%s', /help lists them all" % command,
                       "ADMIN")


# Read the options typed after "python server.py" and start the server.
def main():
    parser = argparse.ArgumentParser(description="LAN chat server")
    parser.add_argument("--host", default="0.0.0.0",
                        help="interface to bind (default: every interface)")
    parser.add_argument("--port", type=int, default=5050, help="TCP port (5050)")
    parser.add_argument("--no-encrypt", action="store_true",
                        help="switch the security layer off")
    arguments = parser.parse_args()
    ChatServer(arguments.host, arguments.port,
               encrypt=not arguments.no_encrypt).start()


# This line means: only run main() if somebody started this file directly.
if __name__ == "__main__":
    main()