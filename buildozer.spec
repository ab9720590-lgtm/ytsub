[app]
title = YTSub
package.name = ytsub
package.domain = org.ytsub
source.dir = .
source.include_exts = py,png,jpg,ttf,xml,json
version = 1.0
requirements = python3==3.13.2,kivy==2.3.0,pyjnius,android,certifi,urllib3,idna,charset-normalizer,requests,defusedxml,beautifulsoup4,soupsieve,deep-translator,youtube-transcript-api,yt-dlp,arabic-[...]
orientation = portrait
fullscreen = 0
android.permissions = INTERNET
android.api = 33
android.minapi = 24
android.archs = arm64-v8a, armeabi-v7a
android.manifest.intent_filters = intent_filters.xml
android.manifest.launch_mode = singleTask
android.accept_sdk_license = True
p4a.branch = v2024.01.21

[buildozer]
log_level = 2
warn_on_root = 1
