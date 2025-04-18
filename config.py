import os
import logging
import configparser

import utils


def initialize():
    global \
        IMG_PATH, \
        DOWNLOAD_PATH, \
        LOG_FILE, \
        LOG_LEVEL, \
        LOG_BACKUP_COUNT, \
        BG_IMG, \
        ICO, \
        status_str, \
        TTS_MODEL, \
        configfile, \
        CONFIG_FILE_PATH, \
        CURRENT_EBOOK_PATH, \
        CURRENT_EBOOK_PART
    INSTALL_DIR = utils.get_main_dir()
    DOWNLOAD_PATH = os.path.join(INSTALL_DIR, "audio")
    IMG_PATH = os.path.join(INSTALL_DIR, "imgs")
    CONFIG_FILE_PATH = os.path.join(INSTALL_DIR, "config.ini")
    configfile = configparser.ConfigParser()

    status_str = "Status"
    LOG_FILE = os.path.join(INSTALL_DIR, "logs", "epubTextToSpeech.log")
    BG_IMG = os.path.join(INSTALL_DIR, "imgs", "TTS Voice Providers.png")
    ICO = os.path.join(INSTALL_DIR, "imgs", "voice_presentation.ico")
    LOG_BACKUP_COUNT = 10  # Keep up to 10 backup logs

    load_config()
    LOG_LEVEL = int(configfile["SETTINGS"]["LOG_LEVEL"])
    TTS_MODEL = configfile["SETTINGS"]["TTS_MODEL"]
    CURRENT_EBOOK_PATH = configfile["CURRENT"]["EBOOK_PATH"]
    CURRENT_EBOOK_PART = int(configfile["CURRENT"]["EBOOK_PART"])


def load_config():
    """Load configuration from INI file."""
    if os.path.exists(CONFIG_FILE_PATH) and os.path.getsize(CONFIG_FILE_PATH) > 0:
        configfile.read(CONFIG_FILE_PATH, encoding='utf-8')
    else:
        configfile["SETTINGS"] = {
            # Python has six log levelswith each one assigned a specific integer indicating the severity of the log.
            # The available log levels in the logging module are listed below in increasing order of severity:
            # NOTSET=0
            # DEBUG=10: used to log messages that are useful for debugging.
            # INFO=20: used to log events within the parameters of expected program behavior.
            # WARN=30: used to log unexpected events which may impede future program function but not severe enough to be an error.
            # ERROR=40: used to log unexpected failures in the program.
            # CRITICAL=50
            "LOG_LEVEL": logging.INFO,
            "TTS_MODEL": "tts_models/fra/fairseq/vits",
        }
        configfile["CURRENT"] = {"EBOOK_PATH": "", "EBOOK_PART": 1}

        save_config()
        load_config()  # Load config at module import


def save_config():
    """Save the current configuration to the INI file."""
    with open(CONFIG_FILE_PATH, "w", encoding="utf-8") as file:
        configfile.write(file)
