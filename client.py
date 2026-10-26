import argparse
import hashlib
import os
import socket
import threading
import time
import tkinter as tk
import wave
from datetime import datetime
from tkinter import filedialog, messagebox, simpledialog

# The shared file. Both the client and the server must have the same copy.
from chat_protocol import (CHUNK_SIZE, CRYPTO_AVAILABLE, IdleTimeout,
                           ProtocolError, human_size, new_key_pair, recv_frame,
                           send_frame, shared_channel)

# appearance
# All the colours in one place. Change these and the whole window changes.
PAPER = "#f6f4ef"  # window background, off white
CARD = "#fffefb"  # panels
EDGE = "#dcd8cf"  # hairlines between panels
INK = "#131313"  # primary text, and the solid buttons
INK_SOFT = "#6f6a62"  # secondary text
HIGHLIGHT = "#eae6dd"  # hover and selection
GOOD = "#1f7a4d"
BAD = "#a32b2b"

TITLE_FONT = ("Segoe UI", 15, "bold")
LABEL_FONT = ("Segoe UI", 9, "bold")
BODY_FONT = ("Segoe UI", 10)
SMALL_FONT = ("Segoe UI", 8)

DOWNLOAD_DIR = "downloads"
SAMPLE_RATE = 16000  # 16000 samples a second is enough for talking
AUDIO_BLOCK = 1024
VIDEO_SIZE = (320, 240)
VIDEO_QUALITY = 55


# media libraries
# These three loaders try to import a library and return None if it is missing.
# That way the chat still works on a laptop where only some things are installed.
def load_audio():
    try:
        import numpy
        import sounddevice
        return sounddevice, numpy
    except Exception:
        return None, None


def load_video():
    try:
        import cv2
        return cv2
    except Exception:
        return None


def load_image_bridge():
    try:
        from PIL import Image, ImageTk
        return Image, ImageTk
    except Exception:
        return None, None


# Never overwrite a file we already have.
# photo.jpg exists, so the next one becomes photo (2).jpg
def unique_path(folder, file_name):
    os.makedirs(folder, exist_ok=True)
    stem, extension = os.path.splitext(os.path.basename(file_name) or "file")
    candidate = os.path.join(folder, stem + extension)
    counter = 2
    while os.path.exists(candidate):
        candidate = os.path.join(folder, "%s (%d)%s" % (stem, counter, extension))
        counter += 1
    return candidate


#  voice recorder
# Records the microphone into a .wav file, which we then send like any file.
class VoiceRecorder:
    MAX_SECONDS = 60

    def __init__(self):
        self.sound_device, self.numpy = load_audio()
        self.stream = None
        self.blocks = []  # the pieces of sound we collected
        self.started_at = 0.0

    @property
    def is_available(self):
        # False when sounddevice is not installed.
        return self.sound_device is not None

    @property
    def is_recording(self):
        return self.stream is not None

    def start(self):
        # Open the microphone. _collect below is called again and again by it.
        self.blocks = []
        self.started_at = time.time()
        self.stream = self.sound_device.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="int16",
            blocksize=AUDIO_BLOCK, callback=self._collect)
        self.stream.start()

    def _collect(self, data, _frames, _timing, _status):
        # Keep each small piece of sound, but stop after one minute.
        if time.time() - self.started_at < self.MAX_SECONDS:
            self.blocks.append(data.copy())

    def stop(self):
        if not self.stream:
            return None, 0  # we were not recording
        self.stream.stop()
        self.stream.close()
        self.stream = None
        if not self.blocks:
            return None, 0  # nothing was recorded
        # Join all the pieces and write them into a real .wav file.
        audio = self.numpy.concatenate(self.blocks)
        path = unique_path(DOWNLOAD_DIR, "voice_note_%s.wav"
                           % datetime.now().strftime("%H%M%S"))
        with wave.open(path, "wb") as wav_file:
            wav_file.setnchannels(1)  # mono
            wav_file.setsampwidth(2)  # 2 bytes per sample
            wav_file.setframerate(SAMPLE_RATE)
            wav_file.writeframes(audio.tobytes())
        return path, len(audio) / float(SAMPLE_RATE)


# call session
# One live call. It sends our microphone and camera out, and plays and draws
# whatever comes back. The same class is used for a normal call and for the
# group call; in a group call more than one person sends us pictures, so the
# video window shows them side by side.
class CallSession:
    def __init__(self, app, peer, media):
        self.app = app
        self.peer = peer  # a name, or "conference"
        self.media = media  # "voice" or "video"
        self.is_running = False
        self.sound_device, self.numpy = load_audio()
        self.open_cv = load_video() if media == "video" else None
        self.image_module, self.image_bridge = load_image_bridge()
        self.microphone = None
        self.speaker = None
        self.camera = None
        self.sequence = 0
        self.frames = {}  # name -> that person's newest picture
        self.video_window = None
        self.video_label = None
        self.video_photo = None  # Tkinter forgets images unless we keep this

    # start up
    def start(self):
        # No sound library means no call at all, so say so and stop.
        if not self.sound_device:
            messagebox.showerror("Calls need extra packages",
                                 "Run:  pip install -r requirements.txt\n\n"
                                 "sounddevice and numpy are needed for audio.")
            return False
        try:
            # Speaker first, then microphone, so we are ready to hear at once.
            self.speaker = self.sound_device.OutputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                blocksize=AUDIO_BLOCK)
            self.speaker.start()
            self.microphone = self.sound_device.InputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                blocksize=AUDIO_BLOCK, callback=self._on_microphone)
            self.microphone.start()
        except Exception as error:
            self.stop()
            messagebox.showerror("Microphone problem",
                                 "%s\n\nCheck that a microphone is connected and "
                                 "that Windows microphone privacy allows it." % error)
            return False

        self.is_running = True
        # For a video call we also try the camera. If there is no camera we
        # carry on with sound only instead of failing.
        if self.media == "video":
            if self.open_cv is None:
                self.app.show_system("camera needs:  pip install opencv-python "
                                     "(continuing with voice only)")
            else:
                self.camera = self._open_camera()
                if self.camera is None:
                    self.app.show_system("no working camera found, continuing "
                                         "with voice only")
                else:
                    threading.Thread(target=self._camera_loop, daemon=True).start()
                self._open_video_window()
        return True

    def _open_camera(self):
        """Try the usual camera indexes; a laptop camera is not always number 0."""
        for index in (0, 1, 2):
            camera = self.open_cv.VideoCapture(index)
            if camera.isOpened():
                # "Opened" is not enough; we check it really gives a picture.
                ready, _frame = camera.read()
                if ready:
                    return camera
            camera.release()
        return None

    # outgoing
    # sounddevice calls this by itself with each new piece of sound.
    def _on_microphone(self, data, _frames, _timing, _status):
        if self.is_running:
            self._send("audio", data.copy().tobytes())

    # Our own thread: take a picture, shrink it, squeeze it, send it, repeat.
    def _camera_loop(self):
        while self.is_running and self.camera is not None:
            ready, frame = self.camera.read()
            if not ready:
                time.sleep(0.05)
                continue
            frame = self.open_cv.resize(frame, VIDEO_SIZE)
            self.frames[self.app.user_name] = frame  # our own small preview
            # Turn the picture into a small JPEG so it fits on the network.
            encoded, buffer = self.open_cv.imencode(
                ".jpg", frame,
                [int(self.open_cv.IMWRITE_JPEG_QUALITY), VIDEO_QUALITY])
            if encoded:
                self._send("video", buffer.tobytes())
            time.sleep(0.06)  # about 16 pictures a second

    # Send one piece of sound or one picture to the server.
    def _send(self, kind, payload):
        self.sequence += 1
        self.app.send({"type": "CALL_MEDIA", "media": kind,
                       "sequence": self.sequence}, payload)

    #  incoming
    def on_media(self, sender, kind, payload):
        """Called on the network thread, so it must not touch any widget."""
        if not self.is_running:
            return
        if kind == "audio" and self.speaker is not None:
            # Play the sound straight away.
            try:
                samples = self.numpy.frombuffer(payload, dtype=self.numpy.int16)
                self.speaker.write(samples.reshape(-1, 1))
            except Exception:
                pass  # a late piece is better dropped than queued
        elif kind == "video" and self.open_cv is not None:
            # Turn the JPEG back into a picture and remember it.
            # The drawing is done elsewhere, by _draw_video.
            picture = self.open_cv.imdecode(
                self.numpy.frombuffer(payload, dtype=self.numpy.uint8),
                self.open_cv.IMREAD_COLOR)
            if picture is not None:
                self.frames[sender] = self.open_cv.resize(picture, VIDEO_SIZE)

    # video window
    # A second, small window that only shows the video.
    def _open_video_window(self):
        window = tk.Toplevel(self.app.root)
        window.title("Call with %s" % self.peer)
        window.configure(bg=INK)
        window.protocol("WM_DELETE_WINDOW", self.app.hang_up)  # X button hangs up
        self.video_label = tk.Label(window, bg=INK, fg=PAPER, font=BODY_FONT,
                                    text="waiting for the camera ...")
        self.video_label.pack(padx=10, pady=10)
        self.video_window = window
        self._draw_video()

    def _draw_video(self):
        """Compose up to four tiles into one picture, 25 times a second."""
        if not self.is_running or self.video_window is None:
            return
        names = sorted(self.frames)[:4]  # at most four people at a time
        if names and self.image_bridge is not None:
            # Write each person's name on their own picture.
            tiles = []
            for name in names:
                tile = self.frames[name].copy()
                self.open_cv.putText(tile, name, (8, VIDEO_SIZE[1] - 10),
                                     self.open_cv.FONT_HERSHEY_SIMPLEX, 0.5,
                                     (255, 255, 255), 1, self.open_cv.LINE_AA)
                tiles.append(tile)
            # Add a black square if the number is odd, so the grid stays neat.
            while len(tiles) % 2 and len(tiles) > 1:
                tiles.append(self.numpy.zeros_like(tiles[0]))
            if len(tiles) == 1:
                grid = tiles[0]
            else:
                # hstack puts pictures side by side, vstack puts rows on top
                # of each other, so together they make a 2 by 2 grid.
                rows = [self.numpy.hstack(tiles[index:index + 2])
                        for index in range(0, len(tiles), 2)]
                grid = self.numpy.vstack(rows)
            # OpenCV keeps colours as BGR but Tkinter wants RGB, so we swap them.
            image = self.image_module.fromarray(
                self.open_cv.cvtColor(grid, self.open_cv.COLOR_BGR2RGB))
            self.video_photo = self.image_bridge.PhotoImage(image=image)
            self.video_label.configure(image=self.video_photo, text="")
        elif names:  # no Pillow: use a plain OpenCV window
            self.open_cv.imshow("Call with %s" % self.peer, self.frames[names[0]])
            self.open_cv.waitKey(1)
        # Ask Tkinter to call this again in 40 milliseconds, which is 25 times
        # a second. We never use a while loop here or the window would freeze.
        self.app.root.after(40, self._draw_video)

    # shut down
    # Close everything we opened. Called when the call ends for any reason.
    def stop(self):
        self.is_running = False
        for stream in (self.microphone, self.speaker):
            try:
                if stream:
                    stream.stop()
                    stream.close()
            except Exception:
                pass
        self.microphone = self.speaker = None
        if self.camera is not None:
            self.camera.release()  # let go of the camera light
            self.camera = None
        if self.open_cv is not None:
            try:
                self.open_cv.destroyAllWindows()
            except Exception:
                pass
        if self.video_window is not None:
            self.video_window.destroy()
            self.video_window = None
        self.frames.clear()


#  the client
class ChatClient:
    """The window, the connection, and the handlers that join the two together."""

    def __init__(self, root, server_host, server_port):
        self.root = root
        self.server_host = server_host
        self.server_port = server_port
        self.sock = None
        self.channel = None  # the encryption box, once agreed
        self.send_lock = threading.Lock()  # one writer at a time
        self.user_name = ""
        self.is_closing = False
        self.users = []  # everyone online
        self.rooms = {}  # room name -> who is in it
        self.history = {}  # one message list per conversation
        self.current = ("global", "")  # which conversation is on screen
        self.transfers = {}  # files arriving right now
        self.transfer_waiters = {}  # files we are waiting to start sending
        self.call = None
        self.pending_call = None  # a call that is ringing, not answered
        self.recorder = VoiceRecorder()
        root.title("LAN Chat")
        root.geometry("1060x680")
        root.minsize(900, 560)
        root.configure(bg=PAPER)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.build_login_screen()  # first screen the user sees

    #  small UI helpers
    # Throw away every widget, so we can draw a different screen.
    def clear_window(self):
        for widget in self.root.winfo_children():
            widget.destroy()

    # One button style used everywhere, so the whole window matches.
    def solid_button(self, parent, text, command, light=False):
        """Black button with off white text, or the quieter inverted version."""
        return tk.Button(parent, text=text, command=command, relief="flat",
                         font=("Segoe UI", 9, "bold"), cursor="hand2",
                         bg=CARD if light else INK, fg=INK if light else PAPER,
                         activebackground=HIGHLIGHT if light else INK_SOFT,
                         activeforeground=INK if light else PAPER,
                         highlightbackground=EDGE, highlightthickness=1 if light else 0,
                         padx=10, pady=5)

    # The small grey heading above a list.
    def section_label(self, parent, text):
        return tk.Label(parent, text=text, font=LABEL_FONT, bg=CARD, fg=INK_SOFT,
                        anchor="w")

    # One list style, used for the rooms list and the online list.
    def plain_list(self, parent, height, on_select):
        box = tk.Listbox(parent, height=height, bg=CARD, fg=INK, relief="flat",
                         font=BODY_FONT, activestyle="none", highlightthickness=1,
                         highlightbackground=EDGE, selectbackground=INK,
                         selectforeground=PAPER, exportselection=False)
        box.bind("<<ListboxSelect>>", on_select)  # run this when a row is clicked
        return box

    #  login screen
    # Screen 1: ask for a name and the server address.
    def build_login_screen(self):
        self.clear_window()
        card = tk.Frame(self.root, bg=CARD, highlightbackground=EDGE,
                        highlightthickness=1, padx=36, pady=32)
        card.place(relx=0.5, rely=0.5, anchor="center", width=420, height=400)
        tk.Label(card, text="LAN CHAT", font=("Segoe UI", 22, "bold"), bg=CARD,
                 fg=INK).pack(anchor="w")
        tk.Label(card, text="real time messaging over your own network",
                 font=SMALL_FONT, bg=CARD, fg=INK_SOFT).pack(anchor="w",
                                                             pady=(2, 24))
        self.section_label(card, "YOUR DISPLAY NAME").pack(fill="x")
        self.name_entry = tk.Entry(card, font=("Segoe UI", 12), bg=PAPER, fg=INK,
                                   relief="flat", insertbackground=INK,
                                   highlightthickness=1, highlightbackground=EDGE)
        self.name_entry.pack(fill="x", ipady=8, pady=(6, 16))
        self.section_label(card, "SERVER IPv4 ADDRESS").pack(fill="x")
        self.host_entry = tk.Entry(card, font=("Segoe UI", 12), bg=PAPER, fg=INK,
                                   relief="flat", insertbackground=INK,
                                   highlightthickness=1, highlightbackground=EDGE)
        self.host_entry.insert(0, self.server_host)  # fill in what was typed at start
        self.host_entry.pack(fill="x", ipady=8, pady=(6, 16))
        self.solid_button(card, "CONNECT", self.connect).pack(fill="x", pady=(8, 10))
        tk.Label(card, text="start server.py on the host laptop first   ·   port %d"
                 % self.server_port, font=SMALL_FONT, bg=CARD,
                 fg=INK_SOFT).pack(anchor="w")
        self.name_entry.focus_set()
        self.root.bind("<Return>", lambda event: self.connect())  # enter key connects

    # main screen
    # Screen 2: the chat itself. Three parts: header, sidebar, chat pane.
    def build_main_screen(self):
        self.clear_window()
        self.root.unbind("<Return>")  # enter now sends messages instead

        # ---- header: the title and whether we are connected ------------------
        header = tk.Frame(self.root, bg=CARD, height=58)
        header.pack(fill="x")
        header.pack_propagate(False)  # keep the height we asked for
        tk.Label(header, text="LAN CHAT", font=("Segoe UI", 13, "bold"), bg=CARD,
                 fg=INK).pack(side="left", padx=18)
        tk.Label(header, text="signed in as %s" % self.user_name, font=SMALL_FONT,
                 bg=CARD, fg=INK_SOFT).pack(side="left")
        self.status_label = tk.Label(header, text="connected", font=SMALL_FONT,
                                     bg=CARD, fg=GOOD)
        self.status_label.pack(side="right", padx=18)
        tk.Frame(self.root, bg=EDGE, height=1).pack(fill="x")  # thin grey line

        body = tk.Frame(self.root, bg=PAPER)
        body.pack(fill="both", expand=True, padx=14, pady=14)

        # ---- sidebar: rooms and the live list of connected clients ----------
        sidebar = tk.Frame(body, bg=CARD, width=236, highlightbackground=EDGE,
                           highlightthickness=1)
        sidebar.pack(side="left", fill="y", padx=(0, 14))
        sidebar.pack_propagate(False)

        # Two fixed conversations at the top: everybody, and the operator.
        self.solid_button(sidebar, "ALL CLIENTS  (global)",
                          lambda: self.open_conversation("global", "")
                          ).pack(fill="x", padx=12, pady=(14, 6))
        self.solid_button(sidebar, "SERVER OPERATOR",
                          lambda: self.open_conversation("server", ""), light=True
                          ).pack(fill="x", padx=12)

        # The rooms list, with buttons to make one or leave one.
        self.section_label(sidebar, "CHAT ROOMS").pack(fill="x", padx=14,
                                                       pady=(18, 6))
        self.room_list = self.plain_list(sidebar, 7, self.on_room_selected)
        self.room_list.pack(fill="x", padx=12)
        room_buttons = tk.Frame(sidebar, bg=CARD)
        room_buttons.pack(fill="x", padx=12, pady=(8, 0))
        self.solid_button(room_buttons, "create / join", self.create_room,
                          light=True).pack(side="left", expand=True, fill="x")
        self.solid_button(room_buttons, "leave", self.leave_room,
                          light=True).pack(side="left", padx=(6, 0))

        # The live list of everyone online. Clicking a name opens a private chat.
        self.section_label(sidebar, "ONLINE CLIENTS").pack(fill="x", padx=14,
                                                           pady=(18, 6))
        self.user_list = self.plain_list(sidebar, 10, self.on_user_selected)
        self.user_list.pack(fill="both", expand=True, padx=12, pady=(0, 14))

        # ---- chat pane: title row, messages, and the typing row -------------
        pane = tk.Frame(body, bg=CARD, highlightbackground=EDGE,
                        highlightthickness=1)
        pane.pack(side="left", fill="both", expand=True)
        title_bar = tk.Frame(pane, bg=CARD, height=56)
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)
        self.title_label = tk.Label(title_bar, text="All clients", font=TITLE_FONT,
                                    bg=CARD, fg=INK)
        self.title_label.pack(side="left", padx=18)
        # The three call buttons, made in a loop because they look the same.
        for text, action in (("voice call", lambda: self.place_call("voice")),
                             ("video call", lambda: self.place_call("video")),
                             ("end call", self.hang_up)):
            self.solid_button(title_bar, text, action, light=True).pack(
                side="right", padx=(0, 8), pady=12)
        tk.Frame(pane, bg=EDGE, height=1).pack(fill="x")

        # The big box where messages appear. It is disabled so the user cannot
        # type into it; we turn it on for a moment whenever we add a line.
        self.transcript = tk.Text(pane, bg=CARD, fg=INK, relief="flat", wrap="word",
                                  font=BODY_FONT, padx=18, pady=14, state="disabled",
                                  insertbackground=INK)
        self.transcript.pack(fill="both", expand=True)
        # Tags are named styles. We use them to colour parts of each message.
        self.transcript.tag_configure("stamp", foreground=INK_SOFT, font=SMALL_FONT)
        self.transcript.tag_configure("sender", foreground=INK,
                                      font=("Segoe UI", 10, "bold"))
        self.transcript.tag_configure("body", foreground=INK, font=BODY_FONT)
        self.transcript.tag_configure("system", foreground=INK_SOFT,
                                      font=("Segoe UI", 9, "italic"))
        self.transcript.tag_configure("alert", foreground=BAD, font=SMALL_FONT)

        # The bottom row: the text box and the send, attach and record buttons.
        tk.Frame(pane, bg=EDGE, height=1).pack(fill="x")
        composer = tk.Frame(pane, bg=CARD, padx=12, pady=10)
        composer.pack(fill="x")
        self.message_entry = tk.Entry(composer, font=("Segoe UI", 11), bg=PAPER,
                                      fg=INK, relief="flat", insertbackground=INK,
                                      highlightthickness=1, highlightbackground=EDGE)
        self.message_entry.pack(side="left", fill="x", expand=True, ipady=9)
        self.message_entry.bind("<Return>", lambda event: self.send_message())
        self.solid_button(composer, "send", self.send_message).pack(side="left",
                                                                    padx=(8, 0))
        self.solid_button(composer, "attach", self.send_file,
                          light=True).pack(side="left", padx=(8, 0))
        self.record_button = self.solid_button(composer, "record voice",
                                               self.toggle_recording, light=True)
        self.record_button.pack(side="left", padx=(8, 0))
        self.message_entry.focus_set()
        self.refresh_sidebar()  # fill the lists in
        self.show_transcript()  # draw whatever we already have

    #  connection
    # The CONNECT button. We check the name, then do the slow part in a thread
    # so the window does not freeze while it waits.
    def connect(self):
        name = self.name_entry.get().strip()
        host = self.host_entry.get().strip() or "127.0.0.1"
        if not name:
            messagebox.showwarning("Name needed", "Type a display name first.")
            return
        self.user_name, self.server_host = name, host
        threading.Thread(target=self.open_connection, daemon=True).start()

    def open_connection(self):
        """Connect, agree a key, log in; all of it off the interface thread."""
        try:
            self.sock = socket.create_connection((self.server_host,
                                                  self.server_port), timeout=8)
            self.sock.settimeout(20.0)
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            # Step 1: the server says hello and sends its public key.
            hello, _ = recv_frame(self.sock)
            private_key, public_hex = ((new_key_pair())
                                       if hello.get("encryption") and CRYPTO_AVAILABLE
                                       else (None, None))
            # Step 2: we send ours back. From now on everything is locked.
            self.raw_send({"type": "KEX", "public_key": public_hex})
            if public_hex and hello.get("public_key"):
                self.channel = shared_channel(private_key, hello["public_key"])
            # Step 3: send our name and wait for a yes or no.
            self.send({"type": "LOGIN", "user_name": self.user_name})
            result, _ = recv_frame(self.sock, self.channel)
            if not result.get("ok"):
                raise ConnectionError(result.get("reason", "the server said no"))
        except Exception as error:
            # root.after sends the work back to the window's own thread, which
            # is the only thread allowed to touch widgets.
            self.root.after(0, self.connection_failed, str(error))
            return
        self.sock.settimeout(None)  # no timeout now, we just wait for messages
        self.root.after(0, self.build_main_screen)
        self.root.after(0, self.show_system, "%s  ·  encryption %s"
                        % (result.get("text", "connected"),
                           "on" if self.channel else "off"))
        # Two background threads: one reads messages, one sends a quiet ping.
        threading.Thread(target=self.receive_loop, daemon=True).start()
        threading.Thread(target=self.keepalive_loop, daemon=True).start()

    def connection_failed(self, reason):
        if self.sock:
            self.sock.close()
            self.sock = None
        messagebox.showerror("Could not connect",
                             "%s:%d\n\n%s" % (self.server_host, self.server_port,
                                              reason))

    def raw_send(self, header, payload=b""):
        """Send before the secure channel exists (the key exchange only)."""
        send_frame(self.sock, self.send_lock, header, payload, None)

    # Normal sending, locked if encryption is on.
    def send(self, header, payload=b""):
        if not self.sock:
            return False
        try:
            send_frame(self.sock, self.send_lock, header, payload, self.channel)
            return True
        except (OSError, ProtocolError):
            self.root.after(0, self.set_status, "connection lost", BAD)
            return False

    def keepalive_loop(self):
        """A quiet ping so the server can tell a dead laptop from a silent one."""
        while self.sock and not self.is_closing:
            time.sleep(30)
            self.send({"type": "PING", "token": time.time()})

    # The main reading thread. It waits for a message, then hands it to the
    # right function below.
    def receive_loop(self):
        handlers = {
            "CHAT": self.on_chat, "SYSTEM": self.on_system_message,
            "STATE": self.on_state, "NOTICE": self.on_notice,
            "STATUS": self.on_status, "SHUTDOWN": self.on_shutdown,
            "ROOM_LEFT": self.on_room_left,
            "FILE_BEGIN": self.on_file_begin, "FILE_CHUNK": self.on_file_chunk,
            "FILE_END": self.on_file_end, "FILE_SENT": self.on_file_sent,
            "FILE_ACCEPTED": self.on_file_accepted,
            "FILE_REJECTED": self.on_file_rejected,
            "CALL_INVITE": self.on_call_invite, "CALL_START": self.on_call_start,
            "CALL_END": self.on_call_end, "CALL_MEMBERS": self.on_call_members,
            "PONG": None,  # nothing to do, the ping worked
        }
        try:
            while not self.is_closing:
                header, payload = recv_frame(self.sock, self.channel)
                message_type = header.get("type")
                if message_type == "CALL_MEDIA":
                    # Sound and video are handled here, on this thread, and not
                    # passed to the window thread. There are about 20 of these
                    # every second, and sending them to the window would make
                    # the screen stutter and the sound arrive late.
                    if self.call:
                        self.call.on_media(header.get("sender", ""),
                                           header.get("media"), payload)
                    continue
                # Everything else changes the screen, so it must go to the
                # window's own thread. That is what root.after(0, ...) does.
                handler = handlers.get(message_type)
                if handler is not None:
                    self.root.after(0, handler, header, payload)
        except (ConnectionError, ProtocolError, IdleTimeout, OSError, ValueError):
            pass  # the connection ended, that is all
        finally:
            if not self.is_closing:
                self.root.after(0, self.set_status, "disconnected", BAD)

    # A chat message arrived. Work out which conversation it belongs to.
    def on_chat(self, header, _payload=b""):
        scope = header.get("scope")
        sender = header.get("sender", "?")
        if scope == "private":
            # The other person is the sender, unless we are the sender.
            other = (header.get("target") if sender == self.user_name else sender)
            # A private line from the operator goes in the Server view, because
            # the operator is not one of the names in the sidebar.
            key = ("server", "") if other == "SERVER" else ("private", other)
        elif scope == "room":
            key = ("room", header.get("room", ""))
        elif scope == "server":
            key = ("server", "")
        else:
            key = ("global", "")
        self.record(key, ("message", sender, header.get("text", ""),
                          header.get("at", time.time())))

    # "x joined the chat" lines.
    def on_system_message(self, header, _payload=b""):
        room = header.get("room")
        key = ("room", room) if room else ("global", "")
        self.record(key, ("system", header.get("text", "")))

    # A small message meant only for us, such as an error.
    def on_notice(self, header, _payload=b""):
        if header.get("level") == "error" and self.pending_call:
            self.pending_call = None  # the ring never turned into a call
        self.record(self.current, ("system", header.get("text", "")))

    # The answer to a status request.
    def on_status(self, header, _payload=b""):
        self.record(self.current, ("system", "server up %s · %d online · %d rooms "
                                   "· %d messages · %s relayed"
                                   % (header.get("uptime"), header.get("online", 0),
                                      header.get("rooms", 0),
                                      header.get("messages", 0),
                                      header.get("relayed", "0 B"))))

    # The server sent a fresh list of users and rooms, so redraw the sidebar.
    def on_state(self, header, _payload=b""):
        self.users = header.get("users", [])
        self.rooms = header.get("rooms", {})
        self.refresh_sidebar()

    # We left a room, so stop showing it.
    def on_room_left(self, header, _payload=b""):
        if self.current == ("room", header.get("room")):
            self.open_conversation("global", "")

    # The server is closing, or we were kicked.
    def on_shutdown(self, header, _payload=b""):
        self.set_status("disconnected", BAD)
        messagebox.showinfo("Server", header.get("text", "the server closed"))

    #  file transfer in
    # A file is starting: open a new file on disk, ready for the pieces.
    def on_file_begin(self, header, _payload=b""):
        path = unique_path(DOWNLOAD_DIR, header.get("file_name", "file.bin"))
        self.transfers[header.get("transfer_id")] = {
            "handle": open(path, "wb"), "path": path, "digest": hashlib.sha256()}
        self.record(self.key_for_incoming(header),
                    ("system", "%s is sending %s (%s) ..."
                     % (header.get("sender"), os.path.basename(path),
                        human_size(header.get("size", 0)))))

    # One piece arrived: write it and add it to the running checksum.
    def on_file_chunk(self, header, payload=b""):
        info = self.transfers.get(header.get("transfer_id"))
        if info:
            info["handle"].write(payload)
            info["digest"].update(payload)

    # The file is finished. Close it and compare our checksum with the sender's;
    # if they match, nothing was lost on the way.
    def on_file_end(self, header, _payload=b""):
        info = self.transfers.pop(header.get("transfer_id"), None)
        if not info:
            return
        info["handle"].close()
        intact = info["digest"].hexdigest() == header.get("sha256")
        self.record(self.key_for_incoming(header),
                    ("system", "saved %s  (%s%s)"
                     % (info["path"], human_size(header.get("size", 0)),
                        ", checksum verified" if intact else ", CHECKSUM MISMATCH")))

    # Our own file finished going out.
    def on_file_sent(self, header, _payload=b""):
        self.record(self.current, ("system", "sent %s (%s) to %s"
                                   % (header.get("file_name"),
                                      human_size(header.get("size", 0)),
                                      header.get("to"))))

    def on_file_accepted(self, header, _payload=b""):
        """The server agreed the route and gave us the transfer id to use."""
        self.wake_sender(header.get("reference"), header.get("transfer_id"))

    # The server refused, for example the person went offline.
    def on_file_rejected(self, header, _payload=b""):
        self.wake_sender(header.get("reference"), None)
        self.record(self.current, ("system", "could not send %s: %s"
                                   % (header.get("file_name"),
                                      header.get("reason"))))

    # Let the waiting sending thread carry on. See stream_file below.
    def wake_sender(self, reference, transfer_id):
        waiter = self.transfer_waiters.get(reference)
        if waiter:
            waiter["transfer_id"] = transfer_id
            waiter["answered"].set()  # this wakes the other thread up

    def key_for_incoming(self, header):
        """Decide which conversation an arriving file belongs in."""
        destination = str(header.get("to", ""))
        if destination.startswith("#"):
            return ("room", destination[1:])
        if destination == "everyone":
            return ("global", "")
        return ("private", header.get("sender", ""))

    # calls in/out
    # Somebody is calling us, or the server is ringing everybody.
    def on_call_invite(self, header, _payload=b""):
        sender = header.get("sender", "?")
        media = header.get("media", "voice")
        if self.call or self.pending_call:
            self.send({"type": "CALL_REJECT"})  # we are busy already
            return
        is_conference = bool(header.get("conference"))
        question = ("The server is ringing everybody for a %s conference.\n\nJoin?"
                    % media) if is_conference else \
                   ("%s is calling you.\n\nAccept the %s call?" % (sender, media))
        if messagebox.askyesno("Incoming %s call" % media, question):
            self.pending_call = media
            self.send({"type": "CALL_ACCEPT"})
        else:
            self.send({"type": "CALL_REJECT"})

    # The call is on. Open the microphone and camera now.
    def on_call_start(self, header, _payload=b""):
        peer = header.get("peer", "?")
        media = header.get("media", "voice")
        self.pending_call = None
        if self.call:
            return
        self.call = CallSession(self, peer, media)
        if not self.call.start():
            # The hardware refused, so tell the other side to stop.
            self.call = None
            self.send({"type": "CALL_END"})
            return
        self.set_status("in a %s call with %s" % (media, peer), GOOD)
        self.show_system("%s call connected with %s" % (media, peer))

    # Who else is in the group call right now.
    def on_call_members(self, header, _payload=b""):
        self.show_system("in the conference: %s"
                         % ", ".join(header.get("members", [])))

    # The call finished, for any reason.
    def on_call_end(self, header, _payload=b""):
        if self.call:
            self.call.stop()
            self.call = None
        self.pending_call = None
        self.set_status("connected", GOOD)
        self.show_system(header.get("reason", "the call ended"))

    # The voice call and video call buttons.
    def place_call(self, media):
        kind, name = self.current
        if self.call or self.pending_call:
            messagebox.showinfo("Already in a call",
                                "End the current call before starting another.")
            return
        # We need to know who to call, so a private chat must be open.
        if kind != "private":
            messagebox.showinfo("Pick somebody to call",
                                "Open a private chat with a client first; the "
                                "server starts group calls with /callall.")
            return
        self.pending_call = media
        self.send({"type": "CALL_INVITE", "to": name, "media": media})
        self.show_system("ringing %s (%s call) ..." % (name, media))

    # The end call button. Also used when the video window is closed.
    def hang_up(self):
        if self.call or self.pending_call:
            self.send({"type": "CALL_END"})
        if self.call:
            self.call.stop()
            self.call = None
        self.pending_call = None
        self.set_status("connected", GOOD)

    #  sending
    # The send button and the enter key. Where it goes depends on which
    # conversation is open at the moment.
    def send_message(self):
        text = self.message_entry.get().strip()
        if not text:
            return
        kind, name = self.current
        if kind == "private":
            self.send({"type": "CHAT", "scope": "private", "target": name,
                       "text": text})
        elif kind == "room":
            self.send({"type": "CHAT", "scope": "room", "room": name, "text": text})
        elif kind == "server":
            self.send({"type": "CHAT", "scope": "server", "text": text})
        else:
            self.send({"type": "CHAT", "scope": "global", "text": text})
        self.message_entry.delete(0, tk.END)  # clear the box

    # The attach button. Also used by the voice note button.
    def send_file(self, path=None, kind="file"):
        """Stream any file in 64 KiB chunks, with a checksum at the end."""
        if path is None:
            path = filedialog.askopenfilename(title="Send a file, image or video")
        if not path:
            return  # the user pressed cancel
        kind = kind if kind != "file" else self.guess_kind(path)
        # Send it to whoever the open conversation belongs to.
        conversation_kind, name = self.current
        destination = {"private": name, "room": "#" + name}.get(conversation_kind,
                                                                "all")
        # A new thread, so a big video does not freeze the window.
        threading.Thread(target=self.stream_file,
                         args=(path, kind, destination), daemon=True).start()

    # Guess from the file ending whether this is an image, a video or sound.
    @staticmethod
    def guess_kind(path):
        extension = os.path.splitext(path)[1].lower()
        if extension in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"):
            return "image"
        if extension in (".mp4", ".mkv", ".avi", ".mov", ".webm"):
            return "video"
        if extension in (".wav", ".mp3", ".m4a", ".ogg", ".opus"):
            return "voice"
        return "file"

    def stream_file(self, path, kind, destination):
        """
        Runs in its own thread: ask the server for a route, then push the file
        through in chunks so a big video never blocks the chat messages.
        """
        # First tell the server what is coming and wait for it to answer with
        # an id. The Event below is how this thread waits for that answer.
        reference = os.urandom(4).hex()
        waiter = {"answered": threading.Event(), "transfer_id": None}
        self.transfer_waiters[reference] = waiter
        self.send({"type": "FILE_BEGIN", "reference": reference,
                   "file_name": os.path.basename(path),
                   "size": os.path.getsize(path), "kind": kind, "to": destination})
        try:
            # Wait up to 10 seconds for the yes or no.
            if not waiter["answered"].wait(10.0) or not waiter["transfer_id"]:
                return  # refused, or the server went quiet
            transfer_id = waiter["transfer_id"]
            digest = hashlib.sha256()  # the checksum, built as we read
            sequence = 0
            with open(path, "rb") as source:
                while True:
                    chunk = source.read(CHUNK_SIZE)
                    if not chunk:
                        break  # end of the file
                    digest.update(chunk)
                    sequence += 1
                    self.send({"type": "FILE_CHUNK", "transfer_id": transfer_id,
                               "sequence": sequence}, chunk)
            # Last message: the checksum, so the other side can check the file.
            self.send({"type": "FILE_END", "transfer_id": transfer_id,
                       "sha256": digest.hexdigest()})
        except OSError as error:
            self.root.after(0, self.show_system,
                            "could not read %s: %s" % (path, error))
        finally:
            self.transfer_waiters.pop(reference, None)  # tidy up

    def toggle_recording(self):
        """One button: first click records the voice note, second click sends it."""
        if not self.recorder.is_available:
            messagebox.showerror("Voice notes need extra packages",
                                 "Run:  pip install -r requirements.txt")
            return
        if self.recorder.is_recording:
            # Second click: stop, then send the .wav like a normal file.
            path, seconds = self.recorder.stop()
            self.record_button.configure(text="record voice", bg=CARD, fg=INK)
            if path:
                self.show_system("voice note recorded (%.1f s), sending ..." % seconds)
                self.send_file(path, kind="voice")
        else:
            # First click: start recording and turn the button black.
            self.recorder.start()
            self.record_button.configure(text="stop and send", bg=INK, fg=PAPER)

    # rooms and users
    # The create / join button: ask for a name and send it.
    def create_room(self):
        room = simpledialog.askstring("Chat room",
                                      "Room name to create or join:",
                                      parent=self.root)
        if room and room.strip():
            self.send({"type": "COMMAND", "command": "create_room",
                       "argument": room.strip()})
            self.open_conversation("room", room.strip().lower())

    # The leave button.
    def leave_room(self):
        kind, name = self.current
        if kind != "room":
            messagebox.showinfo("Pick a room", "Open the room you want to leave.")
            return
        self.send({"type": "COMMAND", "command": "leave_room", "argument": name})

    # A row in the rooms list was clicked.
    def on_room_selected(self, _event):
        selected = self.room_list.curselection()
        if not selected:
            return
        # The row looks like "#project  3 ·joined", so take the first word.
        room = self.room_list.get(selected[0]).split("  ")[0].lstrip("#")
        # If we are not in it yet, join it first.
        if self.user_name not in self.rooms.get(room, []):
            self.send({"type": "COMMAND", "command": "join_room", "argument": room})
        self.open_conversation("room", room)

    # A name in the online list was clicked: open a private chat.
    def on_user_selected(self, _event):
        selected = self.user_list.curselection()
        if selected:
            name = self.user_list.get(selected[0]).strip().split(" ")[0]
            if name != self.user_name:  # clicking ourselves does nothing
                self.open_conversation("private", name)

    # Redraw both sidebar lists from the latest server list.
    def refresh_sidebar(self):
        # This can be called before the main screen exists, so we check first.
        if not hasattr(self, "user_list") or not self.user_list.winfo_exists():
            return
        self.user_list.delete(0, tk.END)
        for name in self.users:
            self.user_list.insert(tk.END, name + ("  (you)" if name == self.user_name
                                                  else ""))
        self.room_list.delete(0, tk.END)
        for room, members in self.rooms.items():
            mark = " ·joined" if self.user_name in members else ""
            self.room_list.insert(tk.END, "#%s  %d%s" % (room, len(members), mark))

    #  transcript output
    # Switch the screen to another conversation and change the title.
    def open_conversation(self, kind, name):
        self.current = (kind, name)
        titles = {"global": "All clients", "server": "Server operator",
                  "room": "# %s" % name, "private": "Private  ·  %s" % name}
        self.title_label.configure(text=titles.get(kind, "Chat"))
        self.show_transcript()

    # Save one line in its conversation, and draw it if that one is on screen.
    # This is why messages in other rooms are not lost while we read this one.
    def record(self, key, entry):
        self.history.setdefault(key, []).append(entry)
        if key == self.current:
            self.draw_entry(entry)

    # A small grey line in the conversation we are looking at.
    def show_system(self, text):
        self.record(self.current, ("system", text))

    # Clear the box and draw the whole saved history of this conversation.
    def show_transcript(self):
        if not hasattr(self, "transcript") or not self.transcript.winfo_exists():
            return
        self.transcript.configure(state="normal")
        self.transcript.delete("1.0", tk.END)
        self.transcript.configure(state="disabled")
        for entry in self.history.get(self.current, []):
            self.draw_entry(entry)

    # Put one line on the screen: time, then name, then the message.
    def draw_entry(self, entry):
        self.transcript.configure(state="normal")  # unlock the box
        if entry[0] == "system":
            self.transcript.insert(tk.END, "·  %s\n" % entry[1], "system")
        else:
            _tag, sender, text, stamp = entry
            clock = datetime.fromtimestamp(stamp).strftime("%H:%M")
            shown = "you" if sender == self.user_name else sender
            self.transcript.insert(tk.END, "%s   " % clock, "stamp")
            self.transcript.insert(tk.END, "%s\n" % shown, "sender")
            self.transcript.insert(tk.END, "%s\n\n" % text, "body")
        self.transcript.configure(state="disabled")  # lock it again
        self.transcript.see(tk.END)  # scroll to the newest line

    # The green or red word in the top right corner.
    def set_status(self, text, color=None):
        if hasattr(self, "status_label") and self.status_label.winfo_exists():
            self.status_label.configure(text=text, fg=color or INK_SOFT)

    # The window X button. Hang up, close the socket, then close the window.
    def close(self):
        self.is_closing = True
        self.hang_up()
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)  # tell the server we left
            except OSError:
                pass
            self.sock.close()
        self.root.destroy()


# Read the options typed after "python client.py", then open the window.
def main():
    parser = argparse.ArgumentParser(description="LAN chat client")
    parser.add_argument("--server", default="127.0.0.1",
                        help="the IPv4 address shown by server.py")
    parser.add_argument("--port", type=int, default=5050, help="TCP port (5050)")
    arguments = parser.parse_args()
    root = tk.Tk()
    ChatClient(root, arguments.server, arguments.port)
    root.mainloop()  # this line keeps the window open


# Only run main() if somebody started this file directly.
if __name__ == "__main__":
    main()