Feature: Catalog page search, filter, pagination, and navigation
  Validates asset search, filter panel opening, pagination navigation, and cross-page navigation to Home and Stewie Assistant.

  Background:
    Given The user is on the Catalog page

  @smoke
  Scenario: Searching 'DQ-AGT-001' returns matching asset row
    When The user enters 'DQ-AGT-001' in the asset search box
    And The user submits the asset search
    Then The first results row contains 'DQ-AGT-001'

  @regression @validation
  Scenario: Searching 'zzzz_nonexistent_asset' shows empty-state message
    When The user enters 'zzzz_nonexistent_asset' in the asset search box
    And The user submits the asset search
    Then The results area shows 'No matching assets'

  @regression @validation
  Scenario: Submitting empty asset search keeps result count unchanged
    When The user clears the asset search box
    And The user submits the asset search
    Then The results row count remains the same as before search

  @smoke
  Scenario: Clicking the Filter button opens the filter panel
    When The user clicks the Filter button
    Then The filter panel becomes visible

  @smoke
  Scenario: Clicking Next advances to page 2 and shows a different first row
    When The user clicks the Next pagination button
    Then The pagination indicator reads 'Page 2'

  @smoke
  Scenario: Clicking Previous returns to page 1 and restores the first row
    When The user clicks the Next pagination button
    And The user clicks the Previous pagination button
    Then The pagination indicator reads 'Page 1'

  @regression @edge
  Scenario: Clicking page 192 disables the Next pagination button
    When The user clicks pagination page 192
    Then The Next pagination button becomes disabled

  @regression @navigation
  Scenario: Clicking Home navigates to the Home page and displays Stewie Terminal landmark
    When The user clicks the Home button
    Then The URL contains '/home'
    And The Home page landmark is visible

  @regression @navigation
  Scenario: Clicking Ask Stewie navigates to Stewie Assistant and shows the assistant landmark
    When The user clicks the Ask Stewie button
    Then The URL contains '/stewie'
    And The Stewie Assistant landmark is visible
