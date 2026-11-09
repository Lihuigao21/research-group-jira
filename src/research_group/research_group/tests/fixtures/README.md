# Presentation validation fixtures

`basic_test_ppt_file.ppt` (15,360 bytes) and `sample.pptx` (47,215 bytes) are
unmodified, real presentation regression files from Apache POI, fixed at commit
`0614835c55f44ab6f3e9b0850ca51e0e53a65a49`. They exercise normal binary PowerPoint
and a complete 38-part OOXML package with two slides, layouts, masters and themes.
They are not substitutes constructed from empty OLE streams or two XML files.

The [upstream binary PPT regression](https://apache.googlesource.com/poi/+/0614835c55f44ab6f3e9b0850ca51e0e53a65a49/poi-scratchpad/src/test/java/org/apache/poi/hslf/TestReWriteSanity.java)
reads the same PPT fixture and verifies the CurrentUser/UserEdit offsets. Original
download URLs, byte sizes and SHA-256 values are in `provenance.json`; the tests
check those hashes. The Apache POI license and notices from the same commit are
retained in `APACHE-POI-LICENSE.txt` and `APACHE-POI-NOTICE.txt` (Apache-2.0).

Corrupt variants are generated in memory by `test_material_formats.py`, leaving
the original fixture bytes unchanged. They cover zero-filled and out-of-bounds
PPT records, broken OPC relationships/targets, package escape paths, and DTDs in
UTF-8/UTF-16/UTF-32 XML. These are minimum structure checks; they do not claim to
reproduce PowerPoint rendering or detect every possible damaged presentation.

Primary format references:

- [MS-PPT PowerPoint Document Stream and persist-directory reconstruction](https://learn.microsoft.com/en-us/openspecs/office_file_formats/ms-ppt/1fc22d56-28f9-4818-bd45-67c2bf721ccf)
- [MS-PPT CurrentUserAtom](https://learn.microsoft.com/en-us/openspecs/office_file_formats/ms-ppt/940d5700-e4d7-4fc0-ab48-fed5dbc48bc1)
- [MS-PPT UserEditAtom](https://learn.microsoft.com/en-us/openspecs/office_file_formats/ms-ppt/3ffb3fab-95de-4873-98aa-d508fbbac981)
- [MS-PPT PersistDirectoryEntry](https://learn.microsoft.com/en-us/openspecs/office_file_formats/ms-ppt/6214b5a6-7ca2-4a86-8a0e-5fd3d3eff1c9)
- [Microsoft OPC parts and relationship resolution](https://learn.microsoft.com/en-us/previous-versions/windows/desktop/opc/parts-overview)
