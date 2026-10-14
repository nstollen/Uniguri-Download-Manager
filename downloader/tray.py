"""Tray icon near the clock while the downloader runs. Plain ctypes, no extra packages.

Hover: tip() (active downloads, total speed). Right-click: a menu; picking an item calls commands[id].
Double-click: the first command (open the download manager).
"""
import ctypes
import os
import sys
from ctypes import wintypes

user32, shell32, kernel32 = ctypes.windll.user32, ctypes.windll.shell32, ctypes.windll.kernel32
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
WM_DESTROY, WM_COMMAND, WM_TIMER, WM_NULL = 0x0002, 0x0111, 0x0113, 0x0000
WM_LBUTTONDBLCLK, WM_RBUTTONUP, WM_TRAY = 0x0203, 0x0205, 0x0400 + 20
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 1, 2, 4
MF_STRING, MF_SEPARATOR = 0, 0x800
TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x2, 0x100
IMAGE_ICON, LR_LOADFROMFILE = 1, 0x10
ICON = os.path.join(os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__)),
                    "udm.ico")  # next to this script, or next to udm-downloader.exe when installed


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256), ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64),
                ("dwInfoFlags", wintypes.DWORD), ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wintypes.HICON)]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR)]


# 64-bit Windows: handles and message parameters must not be cut to 32 bits
user32.DefWindowProcW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
user32.DefWindowProcW.restype = LRESULT
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = (wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                                   wintypes.HINSTANCE, wintypes.LPVOID)
user32.LoadImageW.restype = wintypes.HANDLE
user32.LoadImageW.argtypes = (wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT)
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.AppendMenuW.argtypes = (wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR)
user32.TrackPopupMenu.argtypes = (wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                  wintypes.HWND, wintypes.LPVOID)
user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
user32.SetTimer.argtypes = (wintypes.HWND, ctypes.c_size_t, wintypes.UINT, wintypes.LPVOID)
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
shell32.Shell_NotifyIconW.argtypes = (wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW))

icon_data = NOTIFYICONDATAW()  # the one icon; remove() needs it from any thread


def remove():
    """Take the icon away (quit, or the downloader stops)."""
    if icon_data.hWnd:
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(icon_data))


def run(title, tip, menu, commands):
    """Show the icon and handle its messages until the process ends. Call it in its own thread.
    title: name of the hidden window (tests find it by this). tip(): the hover text, asked every 2 seconds.
    menu: [(id, label)], id 0 = a line. commands: id -> function."""
    # ---- input: a hidden window that receives the icon's mouse messages ----
    instance = kernel32.GetModuleHandleW(None)
    taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")  # Explorer restarted: add the icon again

    def add():
        icon_data.szTip = tip()[:127]
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(icon_data))

    def show_menu(hwnd):
        popup = user32.CreatePopupMenu()
        for item, label in menu:
            user32.AppendMenuW(popup, MF_SEPARATOR if not item else MF_STRING, item, label)
        point = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        user32.SetForegroundWindow(hwnd)  # without this the menu does not close when you click elsewhere
        picked = user32.TrackPopupMenu(popup, TPM_RIGHTBUTTON | TPM_RETURNCMD, point.x, point.y, 0, hwnd, None)
        user32.PostMessageW(hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(popup)
        if picked in commands:
            commands[picked]()

    # ---- process: the messages ----
    def on_message(hwnd, message, wparam, lparam):
        if message == WM_TRAY and lparam == WM_RBUTTONUP:
            show_menu(hwnd)
        elif message == WM_TRAY and lparam == WM_LBUTTONDBLCLK:
            commands[menu[0][0]]()
        elif message == WM_COMMAND and wparam in commands:  # the same commands, posted (tests use this)
            commands[wparam]()
        elif message == WM_TIMER:  # keep the hover text fresh
            icon_data.szTip = tip()[:127]
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(icon_data))
        elif message == taskbar_created:
            add()
        elif message == WM_DESTROY:
            remove()
        else:
            return user32.DefWindowProcW(hwnd, message, wparam, lparam)
        return 0

    proc = WNDPROC(on_message)  # kept in a variable: Windows calls it for as long as the window lives
    window_class = WNDCLASSW(lpfnWndProc=proc, hInstance=instance, lpszClassName="UDMTray")
    user32.RegisterClassW(ctypes.byref(window_class))
    hwnd = user32.CreateWindowExW(0, "UDMTray", title, 0, 0, 0, 0, 0, None, None, instance, None)

    # ---- output: the icon, then the message loop ----
    icon_data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
    icon_data.hWnd, icon_data.uID = hwnd, 1
    icon_data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
    icon_data.uCallbackMessage = WM_TRAY
    icon_data.hIcon = user32.LoadImageW(None, ICON, IMAGE_ICON, user32.GetSystemMetrics(49), user32.GetSystemMetrics(50),
                                        LR_LOADFROMFILE)  # 49, 50: the small icon size for this screen's scaling
    add()
    user32.SetTimer(hwnd, 1, 2000, None)
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))
