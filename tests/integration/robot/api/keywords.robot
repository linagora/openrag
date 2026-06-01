*** Settings ***
Library     String
Library     Collections
Library    RequestsLibrary

*** Variables ***
${BASE}
${PORT}
${test_file_pdf}    ../resources/test_file.pdf
${test_file_txt}    ../resources/test_file.txt
${test_part_1}    test
${test_part_2}    test2
${BASE_URL}       ${BASE}:${PORT}
${AUTH_TOKEN}     none
&{HEADERS}        Authorization=Bearer ${AUTH_TOKEN}

*** Keywords ***
Clean Up Test
    [Arguments]    @{part}
    ${allowed_status}=    Create List    204    404
    FOR    ${partition}    IN    @{part}
        ${response}=    DELETE    ${BASE_URL}/partition/${partition}  headers=${HEADERS}  expected_status=any
        ${status_code}=    Convert To String    ${response.status_code}
        List Should Contain Value    ${allowed_status}    ${status_code}
    END

Get Task Status
    [Arguments]    ${task_id}    ${headers}=${HEADERS}     ${expected_status}=200    
    ${response}=    GET    ${BASE_URL}/indexer/task/${task_id}  headers=${headers}  expected_status=${expected_status}
    RETURN    ${response.json()}

Get Extract
    [Arguments]    ${extract_id}    ${headers}=${HEADERS}     ${expected_status}=200
    ${response}=    GET    ${BASE_URL}/extract/${extract_id}  headers=${headers}  expected_status=${expected_status}
    RETURN    ${response.json()}

Index File Non Blocking
    [Arguments]    ${file_path}    ${id}    ${part}    ${expected_status}=201
    ${file}=    Get File For Streaming Upload    ${file_path}
    ${files}=    Create Dictionary    file=${file}
    ${response}=    POST
    ...    ${BASE_URL}/indexer/partition/${part}/file/${id}
    ...    files=${files}
    ...    headers=${HEADERS}
    ...    expected_status=${expected_status}
    ${response}=    Set Variable    ${response.json()}
    Should Match Regexp    ${response}[task_status_url]    ${BASE_URL}/indexer/task/[a-fA-F0-9]{48}
    RETURN    ${response}

Index File
    [Arguments]    ${file_path}    ${id}    ${part}    ${headers}=${HEADERS}    ${expected_status}=201
    ${file}=    Get File For Streaming Upload    ${file_path}
    ${files}=    Create Dictionary    file    ${file}
    ${response}=    POST
    ...    ${BASE_URL}/indexer/partition/${part}/file/${id}
    ...    files=${files}
    ...    headers=${headers}
    ...    expected_status=${expected_status}
    ${response}=    Set Variable    ${response.json()}
    Should Match Regexp    ${response}[task_status_url]    ${BASE_URL}/indexer/task/[a-fA-F0-9]{48}
    ${task_id}=    Fetch From Right    ${response}[task_status_url]    /
    Sleep    1
    FOR    ${i}    IN RANGE    0    120
        ${response}=    Get Task Status    ${task_id}   headers=${headers}
        IF    '${response}[task_state]' == 'COMPLETED'    BREAK
        Sleep    1
        IF    ${i} == 119
            Log    Task '${task_id}' is still running after 120 seconds.
            Log    ${response}
            Fail    Task '${task_id}' is still running after 120 seconds.
        END
    END

Check File Exists
    [Arguments]    ${id}    ${part}    ${expected_status}=200
    ${response}=    GET    ${BASE_URL}/${part}/file/${id}    headers=${HEADERS}    expected_status=${expected_status}
    ${json}=    Set Variable    ${response.json()}

    IF    '${expected_status}' == '200'
        Dictionary Should Contain Key    ${json}    metadata
        Dictionary Should Contain Key    ${json}    documents
        ${documents}=    Get From Dictionary    ${json}    documents
        Should Be True    ${documents} != []
        FOR    ${doc}    IN    @{documents}
            Dictionary Should Contain Key    ${doc}    link
        END
    END

    IF    '${expected_status}' == '404'
        Should Be Equal As Strings    ${json}[detail]    [VDB_FILE_NOT_FOUND]: File ID '${id}' does not exist in partition '${part}'
    END

Patch File
    [Arguments]    ${id}    ${part}    ${metadata}    ${expected_status}=200
    ${form_data}=    Create Dictionary    metadata=${metadata}
    ${response}=    PATCH
    ...    ${BASE_URL}/indexer/partition/${part}/file/${id}
    ...    data=${form_data}
    ...    headers=${HEADERS}
    ...    expected_status=${expected_status}
    IF    '${expected_status}' == '200'
        Should Be Equal As Strings    ${response.json()}[message]    Metadata for file '${id}' successfully updated.
    END

Delete File
    [Arguments]    ${id}    ${part}=${test_part_1}    ${expected_status}=204
    ${response}=    DELETE    ${BASE_URL}/indexer/partition/${part}/file/${id}  headers=${HEADERS}  expected_status=${expected_status}
    RETURN    None

Delete Partition
    [Arguments]    ${part}    ${headers}=${HEADERS}    ${expected_status}=204
    ${response}=    DELETE
    ...    ${BASE_URL}/partition/${part}
    ...    headers=${headers}
    ...    expected_status=${expected_status}
    RETURN    None

Get File Metadata
    [Arguments]    ${id}    ${part}    &{expected_metadata_and_status}
    ${expected_status}=    Get From Dictionary    ${expected_metadata_and_status}    expected_status    200
    ${expected_metadata}=    Remove From Dictionary    ${expected_metadata_and_status}    expected_status
    ${response}=    GET    ${BASE_URL}/partition/${part}/file/${id}   headers=${HEADERS}   expected_status=${expected_status}
    ${json_response}=    Set Variable    ${response.json()}
    IF    '${expected_status}' == '404'
        Should Be Equal As Strings    ${json_response}[detail]    [VDB_FILE_NOT_FOUND]: File ID '${id}' does not exist in partition '${part}'
    END
    IF    ${expected_metadata}    # Check if expected_metadata is not empty
        FOR    ${key}    IN    @{expected_metadata.keys()}
            Should Be Equal    ${json_response['metadata']['${key}']}    ${expected_metadata['${key}']}
        END
    ELSE
        Log    No expected metadata to validate.
    END

Get Models
    [Arguments]   ${headers}=${HEADERS}
    ${response}=    GET    ${BASE_URL}/v1/models  headers=${headers}
    Log To Console    ${response}

Create User
    [Arguments]   ${display_name}    ${external_user_id}    ${is_admin}=false    ${expected_status}=201
    ${form_data}=    Create Dictionary
    ...    display_name=${display_name}
    ...    external_user_id=${external_user_id}
    ...    is_admin=${is_admin}
    ${response}=    POST
    ...    ${BASE_URL}/users
    ...    headers=${HEADERS}
    ...    data=${form_data}
    ...    expected_status=${expected_status}
    RETURN    ${response.json()}
    
Skip If Auth Disabled
    Run Keyword If    '${AUTH_TOKEN}' == 'none'    Skip   Auth is disabled