import click

from coprs.logic.builds_logic import BuildChrootsLogic
from coprs import models

from copr_common.enums import StatusEnum

import tqdm


@click.command()
def get_pkgs_build_time_data():
    """Get packages build time data."""
    build_chroots = BuildChrootsLogic.get_multiply().filter(
        models.BuildChroot.status == StatusEnum("succeeded")
    ).filter(
        models.Build.pkg_version.is_not(None)
    ).filter(
        models.BuildChroot.started_on.is_not(None)
    ).filter(
        models.BuildChroot.ended_on.is_not(None)
    ).filter(
        models.Copr.deleted.is_(False)
    ).filter(
        models.MockChroot.is_active.is_(True)
    ).all()

    result = []
    for build_chroot in tqdm.tqdm(build_chroots):
        try:
            pkg_version = build_chroot.build.pkg_version
            epoch = pkg_version.split(":")[0] if ":" in pkg_version else 0
            version = pkg_version.split(":")[-1].split("-")[0]
            release = pkg_version.split("-")[-1]

            # TODO: funtion for this...
            path_to_hw_info = build_chroot._compressed_log_variant("hw_info.log", [])
            if path_to_hw_info:
                path_to_hw_info = path_to_hw_info.replace("http://backend_httpd:5002", "")

            result.append(
                {
                    "package_name": build_chroot.build.package.name,
                    #"epoch": build_chroot.results.epoch,
                    #"version": build_chroot.results.version,
                    #"release": build_chroot.results.release,
                    "epoch": epoch,
                    "version": version,
                    "release": release,
                    "mock_chroot_name": build_chroot.mock_chroot.name,
                    "build_duration": build_chroot.ended_on - build_chroot.started_on,
                    # tmp path to the hw info to get the data
                    # a lot of data is already deleted so... this to be removed
                    "path_to_hw_info": path_to_hw_info,
                    "hw_info": {
                        "cpu_model_name": "",
                        "cpu_model": "",
                        "cpu_cores": "",
                        "cpu_arch": "",
                        "ram": "",
                        "swap": "",
                    },
                }
            )
        except Exception as e:
            print(e)
            continue

    import json
    with open("build_time_data.json", "w") as f:
        json.dump(result, f, indent=4)
