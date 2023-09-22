from pyroSAR.snap.auxil import Workflow, gpt, groupbyWorkers
from pyroSAR import identify

import os
import glob
import rasterio as rio
import numpy as np
from rasterio import merge, mask
from rasterio.io import MemoryFile
from rio_cogeo.cogeo import cog_translate
from rio_cogeo.profiles import cog_profiles
from scipy.ndimage import binary_erosion
from s1pro.auxils import get_burst_geometry
from datetime import datetime
import calendar

from .auxils import remove

import logging

log = logging.getLogger(__name__)

# import logging


def S1_simple_proc(
    file_mst,
    file_slv,
    out_dir,
    tmp_dir,
    shp=None,
    pol="full",
    clear_tmp_files=True,
    erosion_width=15,
    apply_ESD=False,
):
    # detailed debug info
    # logging.basicConfig(level=logging.DEBUG)

    if apply_ESD:
        graph_path = "../graph/TOPSAR_esd_coh_geocode_IW_to_geotiff.xml"
    else:
        graph_path = "../graph/TOPSAR_coh_geocode_IW_to_geotiff.xml"

    # retrieve burst geometries
    gdf_burst_mst = get_burst_geometry(
        file_mst, target_subswaths=["IW1", "IW2", "IW3"], polarization="VV"
    )
    gdf_burst_slv = get_burst_geometry(
        file_slv, target_subswaths=["IW1", "IW2", "IW3"], polarization="VV"
    )

    # find what subswaths and bursts intersect AOI
    if shp is not None:
        gdf_burst_mst = gdf_burst_mst[gdf_burst_mst.intersects(shp)]
        gdf_burst_slv = gdf_burst_slv[gdf_burst_slv.intersects(shp)]

    # identify corresponding subswaths
    sel_subsw_mst = gdf_burst_mst["subswath"]
    sel_subsw_slv = gdf_burst_slv["subswath"]
    unique_subswaths = np.unique(np.concatenate((sel_subsw_mst, sel_subsw_slv)))

    # check that polarization is correct
    info_mst = identify(file_mst)
    if isinstance(pol, str):
        if pol == "full":
            pol = info_mst.polarizations
        else:
            if pol in info_mst.polarizations:
                pol = [pol]
            else:
                raise RuntimeError(
                    "polarization {} does not exists in the source product".format(pol)
                )
    elif isinstance(pol, list):
        pol = [x for x in pol if x in info_mst.polarizations]
    else:
        raise RuntimeError("polarizations must be of type str or list")

    # do a check on orbits and slice
    info_slv = identify(file_slv)
    meta_mst = info_mst.scanMetadata()
    meta_slv = info_slv.scanMetadata()
    slnum = meta_mst["sliceNumber"]
    orbnum = meta_mst["orbitNumber_rel"]
    if meta_slv["sliceNumber"] != slnum:
        raise ValueError("Images from two different slices")
    if meta_slv["orbitNumber_rel"] != orbnum:
        raise ValueError("Images from two different orbits")

    # parse dates
    datestr_mst = meta_mst["start"]
    datestr_slv = meta_slv["start"]
    date_mst = datetime.strptime(datestr_mst, "%Y%m%dT%H%M%S")
    date_slv = datetime.strptime(datestr_slv, "%Y%m%dT%H%M%S")
    calendar_mst = f"{date_mst.day}{calendar.month_abbr[date_mst.month]}{date_mst.year}"
    calendar_slv = f"{date_slv.day}{calendar.month_abbr[date_slv.month]}{date_slv.year}"

    # check availability of orbit state vector file
    orbit_type = "Sentinel Precise (Auto Download)"
    match = info_mst.getOSV(osvType="POE", returnMatch=True)  # , osvdir=osvPath)
    match2 = info_slv.getOSV(osvType="POE", returnMatch=True)  # , osvdir=osvPath)
    if match is None or match2 is None:
        print("Precise orbits not available, using restituted")
        info_mst.getOSV(osvType="RES")  # , osvdir=osvPath)
        info_slv.getOSV(osvType="RES")  # , osvdir=osvPath)
        orbit_type = "Sentinel Restituted (Auto Download)"

    for p in pol:
        tmp_names = []
        for subswath in unique_subswaths:
            # setting graph parameters
            wfl = Workflow(graph_path)
            wfl["Read"].parameters["file"] = file_mst
            wfl["Read(2)"].parameters["file"] = file_slv

            print(f"Processing subswath {subswath} in {p} polarization.")
            wfl["TOPSAR-Split"].parameters["subswath"] = subswath
            wfl["TOPSAR-Split(2)"].parameters["subswath"] = subswath

            wfl["TOPSAR-Split"].parameters["selectedPolarisations"] = p
            wfl["TOPSAR-Split(2)"].parameters["selectedPolarisations"] = p

            bursts_mst = gdf_burst_mst[gdf_burst_mst["subswath"] == subswath][
                "burst"
            ].values
            burst_mst_min = bursts_mst.min()
            burst_mst_max = bursts_mst.max()
            wfl["TOPSAR-Split"].parameters["firstBurstIndex"] = burst_mst_min
            wfl["TOPSAR-Split"].parameters["lastBurstIndex"] = burst_mst_max

            bursts_slv = gdf_burst_slv[gdf_burst_slv["subswath"] == subswath][
                "burst"
            ].values
            burst_slv_min = bursts_slv.min()
            burst_slv_max = bursts_slv.max()
            wfl["TOPSAR-Split(2)"].parameters["firstBurstIndex"] = burst_slv_min
            wfl["TOPSAR-Split(2)"].parameters["lastBurstIndex"] = burst_slv_max

            wfl["Apply-Orbit-File"].parameters["orbitType"] = orbit_type
            wfl["Apply-Orbit-File(2)"].parameters["orbitType"] = orbit_type

            wfl["TOPSAR-Deburst"].parameters["selectedPolarisations"] = p

            tmp_name = f"coh_{subswath}_{p}_{calendar_mst}_{calendar_slv}_slice_{slnum}"
            tmp_names.append(tmp_name)
            wfl["Write"].parameters["file"] = f"{tmp_dir}/{tmp_name}_geo.tif"
            wfl.write(f"{tmp_dir}/graph_coh.xml")
            grp = groupbyWorkers(f"{tmp_dir}/graph_coh.xml", n=1)
            gpt(f"{tmp_dir}/graph_coh.xml", groups=grp, tmpdir=tmp_dir)

            print(f"Removing dark edges after terrain correction")
            with rio.open(f"{tmp_dir}/{tmp_name}_geo.tif", "r") as src:
                prof = src.profile.copy()
                prof.update({"driver": "GTiff", "nodata": 0})
                struct = np.ones((erosion_width, erosion_width))
                with rio.open(
                    f"{tmp_dir}/{tmp_name}_geo_border.tif", "w", **prof
                ) as dst:
                    for i in range(1, prof["count"] + 1):
                        band_src = src.read(i)
                        msk_src = band_src != 0
                        msk_dst = binary_erosion(msk_src, struct)
                        band_dst = band_src * msk_dst
                        dst.write(band_dst, i)

        print("Merging and cropping current subswath")
        to_merge = [
            rio.open(f"{tmp_dir}/{tmp_name}_geo_border.tif") for tmp_name in tmp_names
        ]
        arr_merge, trans_merge = merge.merge(to_merge)
        with rio.open(f"{tmp_dir}/{tmp_names[0]}_geo_border.tif") as src:
            prof = src.profile.copy()
        prof.update(
            {
                "height": arr_merge.shape[1],
                "width": arr_merge.shape[2],
                "transform": trans_merge,
                "nodata": 0,
            }
        )

        # crop without writing intermediate file
        if shp is not None:
            with MemoryFile() as memfile:
                with memfile.open(**prof) as mem:
                    # Populate the input file with numpy array
                    mem.write(arr_merge)
                    arr_crop, trans_crop = mask.mask(mem, [shp], crop=True)
                    prof_out = mem.profile.copy()
                    prof_out.update(
                        {
                            "transform": trans_crop,
                            "width": arr_crop.shape[2],
                            "height": arr_crop.shape[1],
                        }
                    )
        else:
            prof_out = prof

        # write as COG
        if shp is not None:
            out_name = f"coh_{p}_{calendar_mst}_{calendar_slv}_slice{slnum}_crop"
        else:
            out_name = f"coh_{p}_{calendar_mst}_{calendar_slv}_slice{slnum}"
        with MemoryFile() as memfile:
            with memfile.open(**prof_out) as mem:
                if shp is not None:
                    mem.write(arr_crop)
                else:
                    mem.write(arr_merge)
                cog_prof = cog_profiles.get("deflate")
                cog_translate(
                    mem,
                    f"{out_dir}/{out_name}.tif",
                    cog_prof,
                    in_memory=True,
                    quiet=True,
                )

        if clear_tmp_files:
            os.remove(f"{tmp_dir}/graph_coh.xml")
            for tmp_name in tmp_names:
                os.remove(f"{tmp_dir}/{tmp_name}_geo.tif")
                os.remove(f"{tmp_dir}/{tmp_name}_geo_border.tif")


def S1_insar_proc(
    file_mst,
    file_slv,
    out_dir,
    tmp_dir,
    shp=None,
    pol="full",
    coh_only = False,
    clear_tmp_files=True,
    erosion_width=15,
    # apply_ESD=False -- maybe for later
):
    # detailed debug info
    # logging.basicConfig(level=logging.DEBUG)

    # if apply_ESD:
    #     raise NotImplementedError("method not implemented")
    # else:
    graph_coreg_path = "../graph/S1-TOPSAR-Coregistration.xml"
    graph_coh_path = "../graph/TOPSAR-Coherence.xml"
    graph_ifg_path = "../graph/TOPSAR-Interferogram.xml"
    graph_tc_path = "../graph/TOPSAR-RD-TerrainCorrection.xml"

    # retrieve burst geometries
    gdf_burst_mst = get_burst_geometry(
        file_mst, target_subswaths=["IW1", "IW2", "IW3"], polarization="VV"
    )
    gdf_burst_slv = get_burst_geometry(
        file_slv, target_subswaths=["IW1", "IW2", "IW3"], polarization="VV"
    )

    # find what subswaths and bursts intersect AOI
    if shp is not None:
        gdf_burst_mst = gdf_burst_mst[gdf_burst_mst.intersects(shp)]
        gdf_burst_slv = gdf_burst_slv[gdf_burst_slv.intersects(shp)]

    # identify corresponding subswaths
    sel_subsw_mst = gdf_burst_mst["subswath"]
    sel_subsw_slv = gdf_burst_slv["subswath"]
    unique_subswaths = np.unique(np.concatenate((sel_subsw_mst, sel_subsw_slv)))

    # check that polarization is correct
    info_mst = identify(file_mst)
    if isinstance(pol, str):
        if pol == "full":
            pol = info_mst.polarizations
        else:
            if pol in info_mst.polarizations:
                pol = [pol]
            else:
                raise RuntimeError(
                    "polarization {} does not exists in the source product".format(pol)
                )
    elif isinstance(pol, list):
        pol = [x for x in pol if x in info_mst.polarizations]
    else:
        raise RuntimeError("polarizations must be of type str or list")

    # do a check on orbits and slice
    info_slv = identify(file_slv)
    meta_mst = info_mst.scanMetadata()
    meta_slv = info_slv.scanMetadata()
    slnum = meta_mst["sliceNumber"]
    orbnum = meta_mst["orbitNumber_rel"]
    if meta_slv["sliceNumber"] != slnum:
        raise ValueError("Images from two different slices")
    if meta_slv["orbitNumber_rel"] != orbnum:
        raise ValueError("Images from two different orbits")

    # parse dates
    datestr_mst = meta_mst["start"]
    datestr_slv = meta_slv["start"]
    date_mst = datetime.strptime(datestr_mst, "%Y%m%dT%H%M%S")
    date_slv = datetime.strptime(datestr_slv, "%Y%m%dT%H%M%S")
    calendar_mst = f"{date_mst.day}{calendar.month_abbr[date_mst.month]}{date_mst.year}"
    calendar_slv = f"{date_slv.day}{calendar.month_abbr[date_slv.month]}{date_slv.year}"

    # check availability of orbit state vector file
    orbit_type = "Sentinel Precise (Auto Download)"
    match = info_mst.getOSV(osvType="POE", returnMatch=True)  # , osvdir=osvPath)
    match2 = info_slv.getOSV(osvType="POE", returnMatch=True)  # , osvdir=osvPath)
    if match is None or match2 is None:
        print("Precise orbits not available, using restituted")
        info_mst.getOSV(osvType="RES")  # , osvdir=osvPath)
        info_slv.getOSV(osvType="RES")  # , osvdir=osvPath)
        orbit_type = "Sentinel Restituted (Auto Download)"

    for p in pol:
        tmp_names = []
        for subswath in unique_subswaths:

            log.info(f"Processing subswath {subswath} in {p} polarization.")

            tmp_name = f"{subswath}_{p}_{calendar_mst}_{calendar_slv}_slice_{slnum}"
            tmp_names.append(tmp_name)

            if not os.path.exists(f"{tmp_dir}/{tmp_name}_coreg.dim"): 
                # TOPS coregistration
                wfl_coreg = Workflow(graph_coreg_path)
                wfl_coreg["Read"].parameters["file"] = file_mst
                wfl_coreg["Read(2)"].parameters["file"] = file_slv

                wfl_coreg["TOPSAR-Split"].parameters["subswath"] = subswath
                wfl_coreg["TOPSAR-Split(2)"].parameters["subswath"] = subswath

                wfl_coreg["TOPSAR-Split"].parameters["selectedPolarisations"] = p
                wfl_coreg["TOPSAR-Split(2)"].parameters["selectedPolarisations"] = p

                bursts_mst = gdf_burst_mst[gdf_burst_mst["subswath"] == subswath][
                    "burst"
                ].values
                burst_mst_min = bursts_mst.min()
                burst_mst_max = bursts_mst.max()
                wfl_coreg["TOPSAR-Split"].parameters["firstBurstIndex"] = burst_mst_min
                wfl_coreg["TOPSAR-Split"].parameters["lastBurstIndex"] = burst_mst_max

                bursts_slv = gdf_burst_slv[gdf_burst_slv["subswath"] == subswath][
                    "burst"
                ].values
                burst_slv_min = bursts_slv.min()
                burst_slv_max = bursts_slv.max()
                wfl_coreg["TOPSAR-Split(2)"].parameters["firstBurstIndex"] = burst_slv_min
                wfl_coreg["TOPSAR-Split(2)"].parameters["lastBurstIndex"] = burst_slv_max

                wfl_coreg["Apply-Orbit-File"].parameters["orbitType"] = orbit_type
                wfl_coreg["Apply-Orbit-File(2)"].parameters["orbitType"] = orbit_type

                wfl_coreg["TOPSAR-Deburst"].parameters["selectedPolarisations"] = p

                wfl_coreg["Write"].parameters["file"] = f"{tmp_dir}/{tmp_name}_coreg"
                wfl_coreg.write(f"{tmp_dir}/graph_coreg.xml")
                grp = groupbyWorkers(f"{tmp_dir}/graph_coreg.xml", n=1)
                gpt(f"{tmp_dir}/graph_coreg.xml", groups=grp, tmpdir=tmp_dir)

            # Coherence computation
            if coh_only:
                if not os.path.exists(f"{tmp_dir}/{tmp_name}_coh.dim"): 
                    wfl_coh = Workflow(graph_coh_path)
                    wfl_coh["Read"].parameters["file"] = f"{tmp_dir}/{tmp_name}_coreg.dim"
                    wfl_coh["Write"].parameters["file"] = f"{tmp_dir}/{tmp_name}_coh"
                    wfl_coh.write(f"{tmp_dir}/graph_coh.xml")
                    gpt(f"{tmp_dir}/graph_coh.xml", tmpdir=tmp_dir)
            else:
                if not os.path.exists(f"{tmp_dir}/{tmp_name}_ifg.dim"): 
                    wfl_ifg = Workflow(graph_ifg_path)
                    wfl_ifg["Read"].parameters["file"] = f"{tmp_dir}/{tmp_name}_coreg.dim"
                    wfl_ifg["Write"].parameters["file"] = f"{tmp_dir}/{tmp_name}_ifg"
                    wfl_ifg.write(f"{tmp_dir}/graph_ifg.xml")
                    gpt(f"{tmp_dir}/graph_ifg.xml", tmpdir=tmp_dir)


            # Terrain correction
            if not os.path.exists(f"{tmp_dir}/{tmp_name}_tc.tif"): 
                wfl_tc = Workflow(graph_tc_path)
                if coh_only:
                    wfl_tc["Read"].parameters["file"] = f"{tmp_dir}/{tmp_name}_coh.dim"
                    wfl_tc["Terrain-Correction"].parameters["outputComplex"] = "false"
                else:
                    wfl_tc["Read"].parameters["file"] = f"{tmp_dir}/{tmp_name}_ifg.dim"
                    wfl_tc["Terrain-Correction"].parameters["outputComplex"] = "true"
                    
                wfl_tc["Write"].parameters["file"] = f"{tmp_dir}/{tmp_name}_tc.tif"
                wfl_tc.write(f"{tmp_dir}/graph_tc.xml")
                grp = groupbyWorkers(f"{tmp_dir}/graph_tc.xml", n=1)
                gpt(f"{tmp_dir}/graph_tc.xml", groups=grp, tmpdir=tmp_dir)

            log.info(f"Removing dark edges after terrain correction")
            with rio.open(f"{tmp_dir}/{tmp_name}_tc.tif", "r") as src:
                prof = src.profile.copy()
                prof.update({"driver": "GTiff", "nodata": 0})
                print(prof)
                struct = np.ones((erosion_width, erosion_width))
                with rio.open(
                    f"{tmp_dir}/{tmp_name}_tc_border.tif", "w", **prof
                ) as dst:
                    for i in range(1, prof["count"] + 1):
                        band_src = src.read(i)
                        msk_src = band_src != 0
                        msk_dst = binary_erosion(msk_src, struct)
                        band_dst = band_src * msk_dst
                        dst.write(band_dst, i)

        log.info(f"Merging and cropping subswath {subswath}")
        to_merge = [
            rio.open(f"{tmp_dir}/{tmp_name}_tc_border.tif") for tmp_name in tmp_names
        ]
        arr_merge, trans_merge = merge.merge(to_merge)
        with rio.open(f"{tmp_dir}/{tmp_names[0]}_tc_border.tif") as src:
            prof = src.profile.copy()
        prof.update(
            {
                "height": arr_merge.shape[1],
                "width": arr_merge.shape[2],
                "transform": trans_merge,
                "nodata": 0,
            }
        )

        # crop without writing intermediate file
        if shp is not None:
            with MemoryFile() as memfile:
                with memfile.open(**prof) as mem:
                    # Populate the input file with numpy array
                    mem.write(arr_merge)
                    arr_crop, trans_crop = mask.mask(mem, [shp], crop=True)
                    prof_out = mem.profile.copy()
                    prof_out.update(
                        {
                            "transform": trans_crop,
                            "width": arr_crop.shape[2],
                            "height": arr_crop.shape[1],
                        }
                    )
        else:
            prof_out = prof.copy()

        if shp is not None:
            if coh_only:
                out_name = f"coh_{p}_{calendar_mst}_{calendar_slv}_slice{slnum}_crop"
            else:
                out_name = f"ifg_{p}_{calendar_mst}_{calendar_slv}_slice{slnum}_crop"
        else:
            if coh_only:
                out_name = f"coh_{p}_{calendar_mst}_{calendar_slv}_slice{slnum}"
            else:
                out_name = f"ifg_{p}_{calendar_mst}_{calendar_slv}_slice{slnum}"
        
        # write COG file
        with rio.open(f"{tmp_dir}/{out_name}.tif", "w", **prof_out) as dst:
            for i in range(0, prof_out["count"]):
                if shp is not None:
                    dst.write(arr_crop[i], i + 1)
                else:
                    dst.write(arr_merge[i], i + 1)
            cog_prof = cog_profiles.get("deflate")
            cog_translate(
                dst,
                f"{out_dir}/{out_name}.tif",
                cog_prof,
                # in_memory=True,
                quiet=True,
            )

        if clear_tmp_files:
            os.remove(f"{tmp_dir}/graph_coreg.xml")
            os.remove(f"{tmp_dir}/graph_coh.xml")
            os.remove(f"{tmp_dir}/graph_ifg.xml")
            os.remove(f"{tmp_dir}/graph_tc.xml")
            files = glob.glob(f'{tmp_dir}/*.data') + glob.glob(f'{tmp_dir}/*.dim')
            for fi in files:
                remove(fi)  


            for tmp_name in tmp_names:
                os.remove(f"{tmp_dir}/{tmp_name}_tc.tif")
                os.remove(f"{tmp_dir}/{tmp_name}_tc_border.tif")


# TODO:
# - write intensities (optional)
# - add some parameters
# - gpt options (?)
# - subswaths as a parameter
# - ESD (optional)
# - break into functions to be reused by other processors if possible
# - slice assembly (post-process with rio)
# - separate phase and coherence, write as 2 geotiffs
# - goldstein filter (optional)
# - processing options: intensity, coherence, phase (True or false for each)
# - use logging instead of print
