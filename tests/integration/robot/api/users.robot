*** Settings ***
Library     String
Library     Collections
Library     RequestsLibrary
Resource    keywords.robot

*** Variables ***
${USERS_ENDPOINT}    ${BASE_URL}/users

*** Test Cases ***

List Users As Admin
    ${response}=    GET    ${USERS_ENDPOINT}    headers=${HEADERS}    expected_status=200
    ${json}=    Set Variable    ${response.json()}
    Dictionary Should Contain Key    ${json}    users
    ${users}=    Get From Dictionary    ${json}    users
    Should Be True    isinstance(${users}, list)

Create User For Tests
    ${display_name}=    Generate Random String    6
    ${external_user_id}=    Generate Random String    12
    ${user}=   Create User   ${display_name}    ${external_user_id}
    Set Suite Variable    ${USER_ID}    ${user}[id]
    Set Suite Variable    ${USER_TOKEN}    ${user}[token]


Get Created User
    ${response}=    GET    ${USERS_ENDPOINT}/${USER_ID}    headers=${HEADERS}    expected_status=200
    ${json}=    Set Variable    ${response.json()}
    Dictionary Should Contain Key    ${json}    id
    Should Be Equal As Strings    ${json}[id]    ${USER_ID}
    Dictionary Should Not Contain Key    ${json}    token

Regenerate Token
    ${response}=    POST    ${USERS_ENDPOINT}/${USER_ID}/regenerate_token    headers=${HEADERS}    expected_status=200
    ${json}=    Set Variable    ${response.json()}
    Dictionary Should Contain Key    ${json}    token
    Should Not Be Equal As Strings    ${json}[token]    ${USER_TOKEN}

Delete User
    ${response}=    DELETE    ${USERS_ENDPOINT}/${USER_ID}    headers=${HEADERS}    expected_status=204

Get Deleted User Should Fail
    ${response}=    GET    ${USERS_ENDPOINT}/${USER_ID}    headers=${HEADERS}    expected_status=404
    ${json}=    Set Variable    ${response.json()}
    Should Contain    ${json}[detail]    VDB_USER_NOT_FOUND

Unauthorized Access Should Fail
    Skip If Auth Disabled
    &{bad_headers}=    Create Dictionary    Authorization=Bearer invalidtoken
    ${response}=    GET    ${USERS_ENDPOINT}    headers=${bad_headers}    expected_status=403
    ${json}=    Set Variable    ${response.json()}
    Should Contain    ${json}[detail]    Invalid token


Delete Default Admin User Should Fail
    ${response}=    DELETE    ${USERS_ENDPOINT}/1   headers=${HEADERS}    expected_status=400
    ${json}=    Set Variable    ${response.json()}
    Should Contain    ${json}[detail]    Cannot delete default admin user