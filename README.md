## Usage:
Clone repository:

`git clone https://cdm-project.com/stabbedbybrick/devine-services.git`

Add folder to `devine.yaml`:

```
directories:
    services: "path/to/services"
```
See help text for each service:

`devine dl SERVICE -?`

## Notes:
If you experience issues with shaka-packager, try downgrading to [v2.6.1](https://github.com/shaka-project/shaka-packager/releases/tag/v2.6.1)