# app.py
import streamlit as st
import pandas as pd
import ifcopenshell
import ifcopenshell.util.unit as ifc_unit
import io
import math

st.set_page_config(page_title="IFC → CSV (Volume extractor)", layout="wide")

st.title("IFC → CSV : Volume & Level extractor")
st.markdown("Upload an IFC file. The app extracts element Type, Name, Level and Volume (where present).")

uploaded = st.file_uploader("Upload .ifc file", type=["ifc"], accept_multiple_files=False)

def build_storey_map(model):
    """Map element GlobalId -> storey name using IfcRelContainedInSpatialStructure."""
    el_to_storey = {}
    for rel in model.by_type("IfcRelContainedInSpatialStructure"):
        storey = rel.RelatingStructure
        storey_name = getattr(storey, "Name", None) or getattr(storey, "LongName", None) or f"Storey_{storey.GlobalId}"
        for related in getattr(rel, "RelatedElements", []):
            el_to_storey[related.GlobalId] = storey_name
    return el_to_storey

def get_volume_from_quantities(element, model):
    """Try to obtain volume from IfcElementQuantity (IfcQuantityVolume). Returns float in model units (converted to m3)."""
    try:
        # Common pattern: element.IsDefinedBy -> IfcElementQuantity -> IfcQuantityVolume
        for rel in getattr(element, "IsDefinedBy", []):
            propdef = getattr(rel, "RelatingPropertyDefinition", None)
            if propdef and propdef.is_a("IfcElementQuantity"):
                for q in getattr(propdef, "Quantities", []):
                    if q.is_a("IfcQuantityVolume"):
                        # attribute name may be 'VolumeValue' or accessible as q.VolumeValue()
                        # Using getattr robustly:
                        val = None
                        if hasattr(q, "VolumeValue"):
                            val = getattr(q, "VolumeValue")
                        elif hasattr(q, "Volume"):
                            val = getattr(q, "Volume")
                        # if value found, convert to m3 using unit utilities
                        if val is not None:
                            # convert to SI (metres) if project units are non-SI
                            length_scale = ifc_unit.calculate_unit_scale(model, "LENGTHUNIT")
                            # volume scale is length_scale ** 3
                            vol_m3 = float(val) * (length_scale ** 3)
                            return vol_m3
    except Exception:
        pass
    return None

def fallback_geometry_volume(element):
    """Fallback placeholder — geometric volume extraction is advanced and may need IfcOpenShell geometry APIs / BlenderBIM.
       Return None to indicate no reliable geometry-derived volume in this simple extractor.
    """
    return None

def extract_elements(model):
    # Build storey map for quick lookup
    storey_map = build_storey_map(model)

    rows = []
    # Iterate all products that are typical build elements
    # You can expand the types list if you want other Ifc types
    candidate_types = [
        "IfcWall", "IfcWallStandardCase", "IfcSlab", "IfcFloor", "IfcColumn",
        "IfcBeam", "IfcRoof", "IfcStair", "IfcFooting", "IfcCovering",
        "IfcBeamStandard", "IfcPlate", "IfcMember", "IfcPile", "IfcOpeningElement"
    ]
    # We'll also include generic IfcProduct if you want a broad sweep
    for t in candidate_types:
        for el in model.by_type(t):
            gid = getattr(el, "GlobalId", None)
            el_type = el.is_a() if hasattr(el, "is_a") else t
            name = getattr(el, "Name", None) or getattr(el, "LongName", None) or ""
            type_name = getattr(el, "ObjectType", None) or getattr(el, "PredefinedType", None) or ""
            # try quantities
            volume = get_volume_from_quantities(el, model)
            if volume is None:
                # fallback attempt (not implemented here)
                volume = fallback_geometry_volume(el)
            # level from containment map
            level = storey_map.get(gid, None)
            rows.append({
                "GlobalId": gid,
                "IfcClass": el_type,
                "TypeName": type_name,
                "Name": name,
                "Level": level,
                "Volume_m3": volume
            })

    # Optionally include all IfcProduct items not covered above
    # This may produce duplicates; comment out if not needed
    for el in model.by_type("IfcProduct"):
        # skip spatial elements (building, storey, zone)
        if el.is_a().startswith("IfcBuilding") or el.is_a().startswith("IfcSite") or el.is_a().startswith("IfcBuildingStorey"):
            continue
        gid = getattr(el, "GlobalId", None)
        # skip if already included
        if any(r["GlobalId"] == gid for r in rows):
            continue
        el_type = el.is_a()
        name = getattr(el, "Name", None) or getattr(el, "LongName", None) or ""
        type_name = getattr(el, "ObjectType", None) or getattr(el, "PredefinedType", None) or ""
        volume = get_volume_from_quantities(el, model)
        level = storey_map.get(gid, None)
        rows.append({
            "GlobalId": gid,
            "IfcClass": el_type,
            "TypeName": type_name,
            "Name": name,
            "Level": level,
            "Volume_m3": volume
        })

    df = pd.DataFrame(rows)
    # Convert Volume column to numeric, keep NaNs
    df["Volume_m3"] = pd.to_numeric(df["Volume_m3"], errors="coerce")
    # Sort and return
    df = df.sort_values(["IfcClass", "Level", "Name"]).reset_index(drop=True)
    return df

if uploaded is not None:
    # Streamlit gives a BytesIO-like object; write to temp buffer and open with ifcopenshell
    with st.spinner("Reading IFC..."):
        try:
            file_bytes = uploaded.read()
            # ifcopenshell expects a filename; it can open from path. We'll write to BytesIO file on disk.
            # Streamlit environment: create in-memory temp - here we write to a temp file
            import tempfile, os
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".ifc")
            tmp.write(file_bytes)
            tmp.flush()
            tmp.close()
            model = ifcopenshell.open(tmp.name)
        except Exception as e:
            st.error(f"Failed to open IFC: {e}")
            st.stop()

    st.success("IFC opened. Extracting elements (may take a while for big files)...")
    with st.spinner("Extracting..."):
        df = extract_elements(model)

    st.write(f"Elements extracted: {len(df)}")
    # show basic stats
    vols_present = df["Volume_m3"].notna().sum()
    st.write(f"Elements with explicit Volume found: {vols_present}")

    # show filters
    col1, col2 = st.columns([2,1])
    with col1:
        ifcclass = st.multiselect("Filter IfcClass", options=sorted(df["IfcClass"].unique()), default=None)
    with col2:
        show_only_with_volume = st.checkbox("Show only elements with volume", value=False)

    filtered = df.copy()
    if ifcclass:
        filtered = filtered[filtered["IfcClass"].isin(ifcclass)]
    if show_only_with_volume:
        filtered = filtered[filtered["Volume_m3"].notna()]

    st.dataframe(filtered, height=500)

    # CSV download
    csv_buffer = io.StringIO()
    filtered.to_csv(csv_buffer, index=False)
    csv_bytes = csv_buffer.getvalue().encode('utf-8')
    st.download_button("Download CSV", data=csv_bytes, file_name="ifc_volumes.csv", mime="text/csv")
