#!/bin/bash
set -e
# Render free plan: use binary wheels, keep safe Pillow 12.2 despite moviepy<12 metadata
pip install --prefer-binary --no-cache-dir --upgrade pip
# install main deps without deps check, then force correct Pillow
pip install --prefer-binary --no-cache-dir -r requirements.txt --no-deps
pip install --prefer-binary --no-cache-dir Pillow==12.2.0
# install moviepy without deps to keep Pillow 12.2 (as per instagrapi METADATA note)
pip install --no-deps --no-cache-dir moviepy==2.2.1
# install remaining video deps that moviepy would have pulled
pip install --prefer-binary --no-cache-dir imageio==2.37.0 proglog==0.1.12 decorator==5.1.1 tqdm==4.66.5 numpy==1.26.4 imageio-ffmpeg==0.6.0 python-dotenv==1.1.0
pip check || true
