var map = L.map("map").setView([19.0760, 72.8777], 11);

L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "© OpenStreetMap"
}).addTo(map);

var marker;


/* =========================================
   UPDATE MAP LOCATION
========================================= */

function updateLocation(lat, lng, landmark, city) {

    if (marker) {
        map.removeLayer(marker);
    }

    marker = L.marker([lat, lng]).addTo(map);

    map.setView([lat, lng], 17);

    // Save coordinates
    document.getElementById("latitude").value = lat;
    document.getElementById("longitude").value = lng;

    // Map-derived location goes into LANDMARK
    document.getElementById("landmark").value = landmark || "";

    // City from map
    document.getElementById("city").value = city || "";

    // IMPORTANT:
    // Do NOT change the address field here.
}


/* =========================================
   REVERSE GEOCODING
========================================= */

function reverseGeocode(lat, lng) {

    fetch(
        "https://nominatim.openstreetmap.org/reverse?format=jsonv2&lat="
        + lat
        + "&lon="
        + lng
    )

    .then(response => response.json())

    .then(data => {

        var landmark = "";
        var city = "";

        if (data.address) {

            // City
            if (data.address.city) {
                city = data.address.city;
            }
            else if (data.address.town) {
                city = data.address.town;
            }
            else if (data.address.village) {
                city = data.address.village;
            }
            else if (data.address.suburb) {
                city = data.address.suburb;
            }
            else if (data.address.county) {
                city = data.address.county;
            }

            // Try to create a useful landmark/location description
            if (data.address.amenity) {
                landmark = data.address.amenity;
            }
            else if (data.address.shop) {
                landmark = data.address.shop;
            }
            else if (data.address.building) {
                landmark = data.address.building;
            }
            else if (data.address.road) {
                landmark = data.address.road;
            }
            else {
                landmark = data.display_name || "";
            }
        }

        updateLocation(
            lat,
            lng,
            landmark,
            city
        );

    })

    .catch(() => {
        alert("Unable to retrieve location details.");
    });
}


/* =========================================
   CLICK ON MAP
========================================= */

map.on("click", function(e) {

    reverseGeocode(
        e.latlng.lat,
        e.latlng.lng
    );

});


/* =========================================
   SEARCH LOCATION
========================================= */

document.getElementById("searchBtn").addEventListener(
    "click",
    function() {

        var location =
            document.getElementById("searchLocation").value.trim();

        if (location === "") {
            alert("Please enter a location.");
            return;
        }

        fetch(
            "https://nominatim.openstreetmap.org/search?format=jsonv2&q="
            + encodeURIComponent(location)
        )

        .then(response => response.json())

        .then(data => {

            if (data.length === 0) {
                alert("Location not found.");
                return;
            }

            var lat = parseFloat(data[0].lat);
            var lng = parseFloat(data[0].lon);

            reverseGeocode(lat, lng);

        })

        .catch(() => {
            alert("Search failed.");
        });

    }
);


/* =========================================
   CURRENT LOCATION
========================================= */

document.getElementById("currentLocationBtn").addEventListener(
    "click",
    function() {

        if (!navigator.geolocation) {
            alert("Geolocation is not supported.");
            return;
        }

        navigator.geolocation.getCurrentPosition(

            function(position) {

                var lat = position.coords.latitude;
                var lng = position.coords.longitude;

                reverseGeocode(lat, lng);

            },

            function() {

                alert("Unable to fetch your current location.");

            }

        );

    }
);