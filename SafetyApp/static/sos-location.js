(function () {
  'use strict';

  var statusPanel = document.getElementById('sosLocationStatus');
  if (!statusPanel) return;

  var stateLabel = document.getElementById('sosLocationState');
  var updatedLabel = document.getElementById('sosLocationUpdatedAt');
  var coordinatesLabel = document.getElementById('sosLatestCoordinates');
  var mapLink = document.getElementById('sosLatestMapLink');
  var updateUrl = statusPanel.getAttribute('data-update-url');
  var statusUrl = statusPanel.getAttribute('data-status-url');
  var lastLatitude = Number(statusPanel.getAttribute('data-latitude'));
  var lastLongitude = Number(statusPanel.getAttribute('data-longitude'));
  var lastUpdatedAt = Date.parse(statusPanel.getAttribute('data-updated-at') || '');
  var lastBrowserSuccessAt = 0;
  var lastAttemptAt = 0;
  var requestPending = false;
  var watchId = null;
  var statusPoll = null;
  var minimumInterval = 15000;
  var maximumInterval = 120000;
  var movementThresholdMeters = 50;

  function setState(value) {
    stateLabel.textContent = value;
  }

  function setUpdatedAt(value) {
    var date = new Date(value);
    if (!isNaN(date.getTime())) {
      updatedLabel.dateTime = date.toISOString();
      updatedLabel.textContent = date.toLocaleString();
    }
  }

  function setSavedLocation(latitude, longitude, updatedAt) {
    if (typeof latitude !== 'number' || typeof longitude !== 'number') return;
    lastLatitude = latitude;
    lastLongitude = longitude;
    coordinatesLabel.textContent = latitude + ', ' + longitude;
    mapLink.href = 'https://www.google.com/maps?q=' + latitude + ',' + longitude;
    mapLink.hidden = false;
    if (updatedAt) {
      lastUpdatedAt = Date.parse(updatedAt) || Date.now();
      setUpdatedAt(lastUpdatedAt);
    }
  }

  function distanceMeters(lat1, lon1, lat2, lon2) {
    var radians = Math.PI / 180;
    var deltaLat = (lat2 - lat1) * radians;
    var deltaLon = (lon2 - lon1) * radians;
    var a = Math.sin(deltaLat / 2) * Math.sin(deltaLat / 2) +
      Math.cos(lat1 * radians) * Math.cos(lat2 * radians) *
      Math.sin(deltaLon / 2) * Math.sin(deltaLon / 2);
    return 6371000 * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
  }

  function stopWatching() {
    if (watchId !== null && navigator.geolocation) {
      navigator.geolocation.clearWatch(watchId);
      watchId = null;
    }
    if (statusPoll !== null) {
      window.clearInterval(statusPoll);
      statusPoll = null;
    }
  }

  function sendPosition(position) {
    var now = Date.now();
    var latitude = position.coords.latitude;
    var longitude = position.coords.longitude;
    var moved = !isFinite(lastLatitude) || !isFinite(lastLongitude) ||
      distanceMeters(lastLatitude, lastLongitude, latitude, longitude) >= movementThresholdMeters;
    var dueByTime = !lastUpdatedAt || now - lastUpdatedAt >= maximumInterval;

    if (requestPending) return;
    if (lastBrowserSuccessAt && now - lastAttemptAt < minimumInterval) return;
    if (lastBrowserSuccessAt && !moved && !dueByTime) {
      setState('Sharing');
      return;
    }

    requestPending = true;
    lastAttemptAt = now;
    var csrfInput = document.querySelector('input[name="csrfmiddlewaretoken"]');
    fetch(updateUrl, {
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': csrfInput ? csrfInput.value : ''
      },
      body: JSON.stringify({ latitude: latitude, longitude: longitude })
    }).then(function (response) {
      if (response.status === 401 || response.status === 409 || response.status === 410) {
        setState('Sharing ended');
        stopWatching();
        return null;
      }
      if (!response.ok) throw new Error('Location update failed');
      return response.json();
    }).then(function (result) {
      if (!result) return;
      lastBrowserSuccessAt = Date.now();
      setSavedLocation(result.latitude, result.longitude, result.updated_at);
      setState('Sharing');
    }).catch(function () {
      setState('Update failed');
    }).finally(function () {
      requestPending = false;
    });
  }

  function handlePositionError(error) {
    if (error.code === error.PERMISSION_DENIED) {
      setState('Permission denied');
    } else if (error.code === error.POSITION_UNAVAILABLE) {
      setState('Location unavailable');
    } else {
      setState('Update failed');
    }
  }

  function refreshSavedLocation() {
    fetch(statusUrl, { credentials: 'same-origin', cache: 'no-store' }).then(function (response) {
      if (response.status === 401 || response.status === 409) {
        setState('Sharing ended');
        stopWatching();
        return null;
      }
      if (!response.ok) throw new Error('Location status unavailable');
      return response.json();
    }).then(function (result) {
      if (!result || result.status !== 'ACTIVE') return;
      if (typeof result.latitude === 'number' && typeof result.longitude === 'number') {
        setSavedLocation(result.latitude, result.longitude, result.updated_at);
      }
    }).catch(function () {
      if (lastBrowserSuccessAt && Date.now() - lastBrowserSuccessAt > maximumInterval) {
        setState('Update failed');
      }
    });
  }

  if (!navigator.geolocation) {
    setState('Location unavailable');
    return;
  }

  watchId = navigator.geolocation.watchPosition(sendPosition, handlePositionError, {
    enableHighAccuracy: true,
    timeout: 15000,
    maximumAge: 5000
  });
  refreshSavedLocation();
  statusPoll = window.setInterval(refreshSavedLocation, 15000);

  window.addEventListener('pagehide', function () {
    stopWatching();
  });
})();
