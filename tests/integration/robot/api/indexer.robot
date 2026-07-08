*** Settings ***
Resource    keywords.robot

*** Test Cases ***
Add File and Delete it
    Index File    ${CURDIR}/${test_file_pdf}    0    test
    Delete File    0    test
    [Teardown]    Clean Up Test    test

Get Non Existent Task Status
    ${response}=    Get Task Status    82891771158d68c1eacb9d1f151391007f68c96901000000     ${HEADERS}     404
    Should Be Equal As Strings
    ...    ${response}[detail]
    ...    Task '82891771158d68c1eacb9d1f151391007f68c96901000000' not found

Add File and Check Metadata
    &{file_metadata}=    Create Dictionary    filename=${test_file_pdf}    partition=test    file_id=0
    Index File    ${CURDIR}/${test_file_txt}    0    test
    ${response}=    Get File Metadata    0    test    &{file_metadata}
    [Teardown]    Clean Up Test    test

Add File and Patch it with new metadata
    Index File    ${CURDIR}/${test_file_txt}    0    test
    ${metadata}=    Create Dictionary    title=Test Title    author=Test Author
    ${metadata}=    Evaluate    json.dumps(${metadata})    json
    Patch File    0    test    ${metadata}
    &{file_metadata}=    Create Dictionary    title=Test Title    author=Test Author
    ${response}=    Get File Metadata    0    test    &{file_metadata}

Move File to New Partition
    [Documentation]    Move a file from one partition to another and verify it no longer exists in the source partition.
    # --- Step 1: Move file to destination partition
    ${metadata}=    Create Dictionary    partition=test1
    ${metadata_json}=    Evaluate    json.dumps(${metadata})    json
    ${form_data}=    Create Dictionary    metadata=${metadata_json}
    ${response}=    PATCH
    ...    ${BASE_URL}/indexer/partition/test/file/0
    ...    headers=${HEADERS}
    ...    data=${form_data}
    ...    expected_status=200
    ${json}=    Set Variable    ${response.json()}
    Dictionary Should Contain Key    ${json}    message

    # --- Step 2: Check that the file exists in destination partition
    ${response}=    GET
    ...    ${BASE_URL}/partition/test1/file/0
    ...    headers=${HEADERS}
    ...    expected_status=200
    # --- Step 3: Verify that file no longer exists in the original partition
    ${response}=    GET
    ...    ${BASE_URL}/partition/test/file/0
    ...    headers=${HEADERS}
    ...    expected_status=404
    ${json}=    Set Variable    ${response.json()}
    Should Contain    ${json}[detail]    does not exist


Get Invalid File Id (`"&é\'-!")    
    Get File Metadata    id="&é\'-!    part=test    expected_status=404


Copy File to Another Partition
    [Documentation]    Index a file into one partition, copy it to another, and verify it exists in the destination.

    # --- Step 1: Copy file to destination partition
    ${form_data}=    Create Dictionary    source_partition=test1    source_file_id=0
    ${response}=    POST
    ...    ${BASE_URL}/indexer/partition/test/file/1/copy
    ...    headers=${HEADERS}
    ...    data=${form_data}
    ...    expected_status=201

    # --- Step 2: Check that the file exists in destination partition
    ${response}=    GET
    ...    ${BASE_URL}/partition/test/file/1
    ...    headers=${HEADERS}
    ...    expected_status=200
    # --- Step 3: Verify that file still exists in the original partition
    ${response}=    GET
    ...    ${BASE_URL}/partition/test1/file/0
    ...    headers=${HEADERS}
    ...    expected_status=200
    ${json}=    Set Variable    ${response.json()}

Cleanup
    ${response}=    DELETE    ${BASE_URL}/partition/test    headers=${HEADERS}    expected_status=204
    ${response}=    DELETE    ${BASE_URL}/partition/test1    headers=${HEADERS}    expected_status=204    

