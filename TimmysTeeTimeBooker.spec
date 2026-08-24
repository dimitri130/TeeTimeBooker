# TimmysTeeTimeBooker.spec
# PyInstaller build spec for TimmysTeeTimeBooker.
#
# Build with:  pyinstaller TimmysTeeTimeBooker.spec
# Output:      dist/TimmysTeeTimeBooker/TimmysTeeTimeBooker.exe
#
# Distribute the entire dist/TimmysTeeTimeBooker/ folder (zip it for GitHub releases).
# On first run, config.json and .secret.key are created next to the exe.
# Chrome must be installed separately on the target machine.
#
# Note: console=False means no console window for the GUI. Output from
# booking/setup runs is captured and shown in the GUI's output panel.
# If you need CLI output when running the exe directly from cmd, set console=True.

from PyInstaller.utils.hooks import collect_data_files

block_cipher = None

a = Analysis(
    ['gui.py'],
    pathex=['.'],
    binaries=[],
    # customtkinter loads its theme/font assets (.json etc.) from the package
    # dir at runtime; a hiddenimport alone bundles only the code.
    datas=collect_data_files('customtkinter'),
    hiddenimports=[
        'customtkinter',
        'undetected_chromedriver',
        'selenium',
        'selenium.webdriver',
        'selenium.webdriver.chrome',
        'selenium.webdriver.chrome.service',
        'selenium.webdriver.common.by',
        'selenium.webdriver.support.ui',
        'selenium.webdriver.support.expected_conditions',
        'cryptography',
        'cryptography.fernet',
        'cryptography.hazmat.primitives',
        'pyautogui',
        'run_setup',
        'crypto_utils',
        'TimmysTeeTimeBooker',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='TimmysTeeTimeBooker',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    # NOT elevated: an elevated process cannot launch/reach the uc Chrome on
    # this machine ("session not created ... chrome not reachable", confirmed
    # 2026-07-13), which broke every driver start. The tscon console-reattach
    # that motivated uac_admin=True will instead run via a pre-authorized
    # elevated scheduled task on the VM (see the VM hosting thread).
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='TimmysTeeTimeBooker',
)
