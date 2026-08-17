"""
This module contains a function which will delete all files and directories created in the gather_images module.
"""

__author__ = """Nicholas Kashani Motlagh @ Ohio State University\n
                Aswathnarayan Radhakrishnan @ Ohio State University\n
                Jim Davis @ Ohio State University (Point of Contact, see __email__)\n
                Roman Ilin @ AFRL/RYAP, Wright-Patterson AFB"""
__email__ = "davis.1719@osu.edu"
__date__ = "2020-08-05"

import shutil

from workspace import OUTPUT_DIR


def reset():
    """
    Will clear all directories and files made by gather_images.py and delete the directories created for each band.
    :return:
    """
    for site in OUTPUT_DIR.glob("*"):
        if "collection" in str(site):
            continue
        for image_dir in site.glob("images/*"):
            if image_dir.is_dir():
                shutil.rmtree(str(image_dir))
            else:
                image_dir.unlink()


if __name__ == "__main__":
    reset()
