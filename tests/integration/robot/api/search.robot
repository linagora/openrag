*** Settings ***
Library     String
Library     Collections
Library     RequestsLibrary
Resource    keywords.robot

*** Variables ***
${SEARCH_ENDPOINT}        ${BASE_URL}/search
${PARTITIONS_ENDPOINT}    ${BASE_URL}/partition
${USERS_ENDPOINT}         ${BASE_URL}/users
${PARTITION_1}            psearch1
${PARTITION_2}            psearch2
${SEARCH_QUERY}           OpenRag

*** Test Cases ***

# --- Setup and Indexing ---

Prepare Partitions And Files
    [Documentation]    Create two partitions, one accessible by admin, one to test restricted access.
    ${response}=    POST    ${PARTITIONS_ENDPOINT}/${PARTITION_1}    headers=${HEADERS}    expected_status=201
    ${response}=    POST    ${PARTITIONS_ENDPOINT}/${PARTITION_2}    headers=${HEADERS}    expected_status=201
    Index File    ${CURDIR}/${test_file_txt}    0    ${PARTITION_1}

Create Regular User
    ${display_name}=    Generate Random String    6
    ${external_id}=     Generate Random String    8
    ${form_data}=    Create Dictionary
    ...    display_name=${display_name}
    ...    external_user_id=${external_id}
    ${response}=    POST
    ...    ${USERS_ENDPOINT}
    ...    headers=${HEADERS}
    ...    data=${form_data}
    ...    expected_status=201
    ${user}=    Set Variable    ${response.json()}
    Set Suite Variable    ${USER_ID}    ${user}[id]
    Set Suite Variable    ${USER_TOKEN}    ${user}[token]

Grant User Access To Partition 1
    ${form_data}=    Create Dictionary
    ...    user_id=${USER_ID}
    ...    role=viewer
    ${response}=    POST
    ...    ${PARTITIONS_ENDPOINT}/${PARTITION_1}/users
    ...    headers=${HEADERS}
    ...    data=${form_data}
    ...    expected_status=201


# --- SEARCH TESTS ---

Search All Partitions As Admin
    [Documentation]    Admin can search across all partitions.
    ${params}=    Create Dictionary
    ...    partitions=all
    ...    text=${SEARCH_QUERY}
    ...    top_k=3
    ${response}=    GET    ${SEARCH_ENDPOINT}    headers=${HEADERS}    params=${params}    expected_status=200
    ${json}=    Set Variable    ${response.json()}
    Dictionary Should Contain Key    ${json}    documents
    ${docs}=    Get From Dictionary    ${json}    documents
    Should Be True    ${docs} != []
    Should Contain    ${docs[0]}[content]    ${SEARCH_QUERY}

Search Multiple Partitions As Admin
    [Documentation]    Admin searches across specific partitions using multiple 'partitions=' query params.
    ${partitions_list}=    Create List    ${PARTITION_1}    ${PARTITION_2}
    ${params}=    Create Dictionary
    ...    partitions=${partitions_list}
    ...    text=${SEARCH_QUERY}
    ...    top_k=5
    ${response}=    GET    ${SEARCH_ENDPOINT}    headers=${HEADERS}    params=${params}    expected_status=200
    ${json}=    Set Variable    ${response.json()}
    Dictionary Should Contain Key    ${json}    documents
    ${docs}=    Get From Dictionary    ${json}    documents
    Should Be True    ${docs} != []
    Should Contain    ${docs[0]}[content]    ${SEARCH_QUERY}


Search Multiple Partitions As User With Unauthorized Access
    [Documentation]    Regular user searches across two partitions, one of which they cannot access → should get 403.
    Skip If Auth Disabled
    &{user_headers}=    Create Dictionary    Authorization=Bearer ${USER_TOKEN}
    ${partitions_list}=    Create List    ${PARTITION_1}    ${PARTITION_2}
    ${params}=    Create Dictionary
    ...    partitions=${partitions_list}
    ...    text=${SEARCH_QUERY}
    ${response}=    GET    ${SEARCH_ENDPOINT}    headers=${user_headers}    params=${params}    expected_status=403


Search Multiple Partitions As User
    [Documentation]    Regular user should only see results in partitions they have access to.
    Skip If Auth Disabled
    &{user_headers}=    Create Dictionary    Authorization=Bearer ${USER_TOKEN}
    ${params}=    Create Dictionary
    ...    partitions=all
    ...    text=${SEARCH_QUERY}
    ${response}=    GET    ${SEARCH_ENDPOINT}    headers=${user_headers}    params=${params}    expected_status=200
    ${json}=    Set Variable    ${response.json()}
    ${docs}=    Get From Dictionary    ${json}    documents
    Should Be True    ${docs} != []
    ${first_doc}=    Get From List    ${docs}    0
    ${meta}=    Get From Dictionary    ${first_doc}    metadata
    Should Be Equal As Strings    ${meta}[partition]    ${PARTITION_1}

Search Multiple Partitions As User Without Access
    [Documentation]    When user explicitly queries a partition they don’t own, expect 403 or empty results.
    Skip If Auth Disabled
    &{user_headers}=    Create Dictionary    Authorization=Bearer ${USER_TOKEN}
    ${params}=    Create Dictionary
    ...    partitions=${PARTITION_2}
    ...    text=${SEARCH_QUERY}
    ${response}=    GET    ${SEARCH_ENDPOINT}    headers=${user_headers}    params=${params}    expected_status=403


Search One Partition As Admin
    ${params}=    Create Dictionary
    ...    text=${SEARCH_QUERY}
    ...    top_k=2
    ${response}=    GET    ${SEARCH_ENDPOINT}/partition/${PARTITION_1}    headers=${HEADERS}    params=${params}    expected_status=200
    ${json}=    Set Variable    ${response.json()}
    ${docs}=    Get From Dictionary    ${json}    documents
    Should Be True    ${docs} != []


Search Specific File As Admin
    ${params}=    Create Dictionary
    ...    text=${SEARCH_QUERY}
    ${response}=    GET
    ...    ${SEARCH_ENDPOINT}/partition/${PARTITION_1}/file/0
    ...    headers=${HEADERS}
    ...    params=${params}
    ...    expected_status=200
    ${json}=    Set Variable    ${response.json()}
    ${docs}=    Get From Dictionary    ${json}    documents
    Should Be True    ${docs} != []


# --- CLEANUP ---

Cleanup Partitions
    ${response}=    DELETE    ${PARTITIONS_ENDPOINT}/${PARTITION_1}    headers=${HEADERS}    expected_status=204
    ${response}=    DELETE    ${PARTITIONS_ENDPOINT}/${PARTITION_2}    headers=${HEADERS}    expected_status=204

Cleanup User
    ${response}=    DELETE    ${USERS_ENDPOINT}/${USER_ID}    headers=${HEADERS}    expected_status=204