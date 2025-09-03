A collection of non-premium services for Devine and Unshackle.

## Usage:
Clone repository:

`git clone https://github.com/stabbedbybrick/services.git`

Add folder to `devine.yaml` or `unshackle.yaml`:

```
directories:
    services: "path/to/services"
```
See help text for each service:

`devine dl SERVICE -?`

## Notes:
Some versions of the dependencies work better than others. These are the recommended versions as of 25/04/08:

- Shaka Packager: [v2.6.1](https://github.com/shaka-project/shaka-packager/releases/tag/v2.6.1)
- CCExtractor: [v0.93](https://github.com/CCExtractor/ccextractor/releases/tag/v0.93)
- MKVToolNix: [v91.0](https://mkvtoolnix.download/downloads.html)
- FFmpeg: [v7.1.1](https://ffmpeg.org/download.html)