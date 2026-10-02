var map = L.map('map').setView([19.0760,72.8777],10);

L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{

    maxZoom:19,

    attribution:'© OpenStreetMap'

}).addTo(map);

var marker;

function updateLocation(lat,lng,address){

    if(marker){

        map.removeLayer(marker);

    }

    marker=L.marker([lat,lng]).addTo(map);

    map.setView([lat,lng],15);

    document.getElementById("latitude").value=lat;

    document.getElementById("longitude").value=lng;

    document.getElementById("address").value=address;

}

map.on("click",function(e){

    fetch(`https://nominatim.openstreetmap.org/reverse?format=jsonv2&lat=${e.latlng.lat}&lon=${e.latlng.lng}`)

    .then(response=>response.json())

    .then(data=>{

        updateLocation(

            e.latlng.lat,

            e.latlng.lng,

            data.display_name

        );

    });

});

document.getElementById("searchBtn").onclick=function(){

    var place=document.getElementById("searchLocation").value;

    if(place=="") return;

    fetch(`https://nominatim.openstreetmap.org/search?format=json&q=${encodeURIComponent(place)}`)

    .then(response=>response.json())

    .then(data=>{

        if(data.length>0){

            updateLocation(

                parseFloat(data[0].lat),

                parseFloat(data[0].lon),

                data[0].display_name

            );

        }

        else{

            alert("Location not found");

        }

    });

};


/* Use the visitor's current location */

document.getElementById("currentLocationBtn").addEventListener("click", function () {

    if (!navigator.geolocation) {

        foodresqAlert(
            "Your browser does not support location detection. Please search for your area or pick it on the map instead.",
            { type: "warning", title: "Location Unavailable" }
        );

        return;

    }

    navigator.geolocation.getCurrentPosition(

        function (position) {

            var lat = position.coords.latitude;
            var lng = position.coords.longitude;

            fetch(`https://nominatim.openstreetmap.org/reverse?format=jsonv2&lat=${lat}&lon=${lng}`)

            .then(response => response.json())

            .then(data => {

                updateLocation(
                    lat,
                    lng,
                    data.display_name || `${lat}, ${lng}`
                );

            })

            .catch(function () {

                updateLocation(lat, lng, `${lat}, ${lng}`);

            });

        },

        function () {

            foodresqAlert(
                "We could not access your location. Please allow location permission, or pick your area on the map.",
                { type: "warning", title: "Location Blocked" }
            );

        }

    );

});